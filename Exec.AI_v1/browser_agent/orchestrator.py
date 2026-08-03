"""The agent loop: observe -> act -> verify, with escalation and re-planning.

`Agent` holds a persistent browser + session so goals can be run back-to-back
(the chat use case): the browser stays on whatever page the last task left it,
and a short transcript is fed forward so follow-ups ("now summarize that") work.

Recovery ladder (FR-MODEL-3, FR-ACT-3, §9):
failed verify -> retry-with-wait -> escalate Sonnet->Opus -> re-plan -> ask_human.
"""
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl

from . import config as cfg
from .browser import BrowserDriver
from .llm import LLM
from .memory import Memory, redact
from .perception import render_observation, signature


def _plan_text(plan):
    steps = plan.get("steps", [])
    lines = [
        f"{i + 1}. {s['description']}  (done when: {s.get('success_criteria', '')})"
        for i, s in enumerate(steps)
    ]
    if plan.get("notes"):
        lines.append(f"Notes: {plan['notes']}")
    return "\n".join(lines) if lines else "(no explicit plan)"


def _history_text(hist):
    if not hist:
        return "(none yet)"
    out = []
    for h in hist[-cfg.HISTORY_WINDOW:]:
        line = f"- step {h['step']}: {h['action']}({json.dumps(h['args'])}) -> {h['result']}"
        if h.get("verified") is False:
            line += f"  [verify FAILED: {h.get('vreason', '')}]"
        if h.get("read_text"):
            line += "\n    read: " + h["read_text"][:600]
        out.append(line)
    return "\n".join(out)


def _route_text(r):
    flags = []
    if r.get("requires_browser"):
        flags.append("browser")
    if r.get("requires_user_approval"):
        flags.append("needs approval")
    tail = f"  [{', '.join(flags)}]" if flags else "  [no web action]"
    site = f" · {r['site']}" if r.get("site") else ""
    return (f"Route: intent={r.get('intent')} → {r.get('tool')}{site}{tail}\n"
            f"       {r.get('rationale', '')}")


# --- the learning layer (procedural memory) --------------------------------
# After a goal finishes we distill its trajectory into a page-independent
# "recipe" (the winning path) plus the distinct verify-failure lessons, key it
# by (intent, site), and feed it back to the planner next time. Element ids are
# per-page and useless later, so a step is generalized to its action + the
# element's accessible name; navigate URLs keep host+path+param-keys but drop
# the values (which carry this run's specifics, e.g. a particular recipient).

def _generalize_url(url):
    try:
        u = urlsplit(url or "")
        if not u.netloc:
            return url
        base = f"{u.scheme or 'https'}://{u.netloc}{u.path}".rstrip("/")
        keys = [k for k, _ in parse_qsl(u.query)]
        return base + (f" (params: {', '.join(dict.fromkeys(keys))})" if keys else "")
    except Exception:
        return url


def _norm_site(site, url):
    """Prefer the router's site label; fall back to the page host."""
    s = (site or "").strip().lower()
    if s:
        return s
    try:
        host = urlsplit(url or "").netloc.lower().split(":")[0]
        return (host[4:] if host.startswith("www.") else host) or "web"
    except Exception:
        return "web"


def _distill(hist):
    """Trajectory -> (recipe, failure_notes). The recipe keeps only the steps
    that moved the task forward (verified mutations, navigations); reads,
    scrolls, and waits are omitted as noise. Failure notes are the distinct
    verify-failure reasons, so a lesson learned the hard way is kept."""
    recipe, notes = [], []
    for h in hist:
        act = h.get("action")
        if h.get("verified") is False and h.get("vreason"):
            notes.append(h["vreason"].strip())
        if act == "navigate":
            recipe.append(f"navigate → {_generalize_url((h.get('args') or {}).get('url', ''))}")
        elif act in ("click", "type", "select") and h.get("verified"):
            tgt = (h.get("target") or "").strip()
            recipe.append(f"{act} “{tgt}”" if tgt else act)
    # collapse immediate repeats (a retried click that finally verified)
    deduped = [s for i, s in enumerate(recipe) if i == 0 or s != recipe[i - 1]]
    return deduped, list(dict.fromkeys(notes))


def _lessons_text(skill):
    """Render a recalled skill as a compact block appended to the planner's
    input. Empty string when there's nothing learned yet."""
    if not skill or (not skill.get("recipe") and not skill.get("failure_notes")):
        return ""
    out = ["\n\nLESSONS FROM PAST RUNS (procedural memory — reuse what worked, "
           "avoid what failed):"]
    if skill.get("recipe"):
        rate = f"{skill['successes']}/{skill['attempts']}"
        best = skill.get("best_steps")
        tail = f", best {best} steps" if best else ""
        out.append(f"Winning path last time (succeeded {rate}{tail}):")
        out += [f"  {i + 1}. {s}" for i, s in enumerate(skill["recipe"])]
    if skill.get("failure_notes"):
        out.append("Known pitfalls to avoid:")
        out += [f"  - {n}" for n in skill["failure_notes"]]
    return "\n".join(out)


_COMMIT_RE = [re.compile(p, re.I) for p in cfg.COMMIT_PATTERNS]
# Interpret an approval reply by intent, not exact match — but let any negation
# win, so "don't send" / "no, cancel" are never read as approval.
_NO_WORDS = ("no", "dont", "don't", "cancel", "stop", "wait", "nope", "abort",
             "never", "hold", "nvm", "nevermind")
_YES_WORDS = ("approve", "approved", "yes", "yeah", "yep", "yup", "ok", "okay",
              "sure", "send", "go ahead", "go", "do it", "confirm", "proceed",
              "sounds good", "please do", "click send")


def _element(scan, eid):
    for e in scan.get("elements", []):
        if e.get("id") == eid:
            return e
    return {}


def _element_name(scan, eid):
    return _element(scan, eid).get("name", "")


# Heuristic for "the human just pasted a credential where it doesn't belong":
# a single token, 6-64 chars, letters+digits, no spaces/@/url. Deliberately
# conservative so real one-word answers (names, "continue") aren't redacted.
_SECRETISH = re.compile(r"^(?=.{6,64}$)(?=.*[A-Za-z])(?=.*\d)\S+$")


def _looks_like_secret(t):
    t = (t or "").strip()
    if not t or " " in t or "@" in t or t.lower().startswith("http"):
        return False
    return bool(_SECRETISH.match(t))


def _scrub(ans, log):
    """Redact a chat reply that looks like a pasted credential (FR-SEC-1)."""
    if _looks_like_secret(ans):
        log("   [!] that looked like a password/secret — I did NOT store or send "
            "it. Enter credentials in the browser window, never in the chat.")
        return "[redacted: user entered a credential directly in the browser]"
    return ans


def _looks_irreversible(name):
    return bool(name) and any(r.search(name) for r in _COMMIT_RE)


def _is_yes(text):
    t = (text or "").strip().lower()
    if not t:
        return False
    if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in _NO_WORDS):
        return False  # any negation cancels
    return any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in _YES_WORDS)


def _ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return "(no console input available)"


class Agent:
    """A persistent browser session you can run goals against repeatedly."""

    def __init__(self, headless=True, start_url=None, on_log=None, on_ask=None,
                 profile=None, channel=None, cdp_url=None):
        # progress + human-handoff are routable: the CLI prints/inputs, the web
        # UI pushes to the page. Defaults keep run.py / chat.py unchanged.
        self._log = on_log or (lambda s: print(s))
        self._ask_fn = on_ask or (lambda q: _ask((q + "\n" if q else "") + "  your reply > "))
        self.llm = LLM()
        self.mem = Memory(cfg.SESSIONS_ROOT / "episodic.db")
        self.session_id = uuid.uuid4().hex[:12]
        self.sdir = cfg.SESSIONS_ROOT / self.session_id
        (self.sdir / "shots").mkdir(parents=True, exist_ok=True)
        (self.sdir / "ax").mkdir(parents=True, exist_ok=True)
        # A named profile persists login across runs (FR-SEC-2). Resolve a bare
        # name to a dir under PROFILES_ROOT; an absolute/relative path is used as-is.
        udir = None
        self.profile = profile
        if profile:
            p = Path(profile)
            udir = p if (p.is_absolute() or p.parts[:1] in ((".",), ("..",))) else cfg.PROFILES_ROOT / profile
        self.channel = channel or cfg.DEFAULT_CHANNEL
        self.driver = BrowserDriver(headless=headless, user_data_dir=udir,
                                    channel=self.channel, cdp_url=cdp_url)
        self.driver.start()
        if cdp_url:
            self._log(f"Attached to your running Chrome at {cdp_url} — using your "
                      "existing logged-in session (I won't close your browser).")
        elif profile:
            self._log(f"Using persistent profile '{profile}' at {udir} — "
                      "log in once here and the session is reused next run.")
        # In attach mode with no start_url, don't hijack the user's active tab.
        if start_url:
            try:
                self.driver.goto(start_url)
            except Exception as e:
                print(f"[warn] start URL failed: {e}")
        self.transcript = []   # [{goal, result}] — conversational context
        self.step = 0          # global step counter across all goals
        self.last_steps = 0
        self.last_replans = 0
        self.last_route = None  # the most recent QUILL routing envelope
        self.mem.start_session(self.session_id, "(interactive session)", {})

    # --- navigation helpers ------------------------------------------------
    def current_url(self):
        try:
            return self.driver.scan().get("url")
        except Exception:
            return None

    def open(self, url):
        if not url.startswith("http"):
            url = "https://" + url
        self.driver.goto(url)

    def cost(self):
        return self.llm.cost()

    def _transcript_text(self):
        if not self.transcript:
            return ""
        out = ["EARLIER IN THIS SESSION:"]
        for t in self.transcript[-4:]:
            out.append(f"- you asked: {t['goal']}")
            out.append(f"  result: {t['result'][:300]}")
        return "\n".join(out) + "\n\n"

    # --- QUILL intent/action router (runs once per request) ----------------
    def route(self, user_request):
        """Classify a request into the QUILL envelope without executing it."""
        r = self.llm.route(user_request, self._transcript_text())
        self.last_route = r
        return r

    # --- approval gate (P2) ------------------------------------------------
    def _require_approval(self, summary, details=""):
        """Block on human confirmation before an irreversible action.
        Returns True only on an explicit yes."""
        if not cfg.REQUIRE_APPROVAL:
            return True
        self._log(f"[approval needed] {summary}")
        prompt = ("APPROVAL NEEDED — " + summary
                  + (("\n\n" + details) if details else "")
                  + "\nReply 'approve' to proceed, or anything else to cancel.")
        ans = self._ask_fn(prompt)
        ok = _is_yes(ans)
        self._log("   approved" if ok else f"   declined ({ans!r})")
        return ok

    # --- the loop ----------------------------------------------------------
    def run_goal(self, goal):
        ctx = self._transcript_text()

        # Route first: decide whether a web action is even needed, and whether
        # it would need approval. Persist as a QUILL event (PRD data model).
        route = self.llm.route(goal, ctx)
        self.last_route = route
        self._log(_route_text(route))
        try:
            self.mem.log_event(self.session_id, goal, route)
        except Exception:
            pass

        # No web action needed (a memory/conversational question): answer directly.
        if not route.get("requires_browser", True):
            ans = self.llm.direct_answer(goal, ctx)
            self.transcript.append({"goal": goal, "result": ans})
            self.last_steps, self.last_replans = 0, 0
            return ans, "answered_no_browser"

        approval_note = ""
        if route.get("requires_user_approval"):
            approval_note = (
                f"\n\n[Note: '{route.get('intent')}' can need approval before an "
                "irreversible step (send/submit/buy/delete). I prepare and draft "
                "up to that point and pause — reply 'approve' when you want me to "
                "commit it.]")

        # Learning layer: recall what worked (and what failed) for this kind of
        # task on this site, and hand it to the planner. intent+site are stable
        # keys across runs; the recipe/pitfalls make the plan shorter and dodge
        # past mistakes (fewer steps, lower cost, no repeated errors).
        intent = route.get("intent") or "unknown"
        site = _norm_site(route.get("site"), self.current_url())
        lessons = ""
        try:
            skill = self.mem.recall_skill(intent, site)
            lessons = _lessons_text(skill)
            if lessons:
                self._log(f"   recalled a learned playbook for {intent}@{site} "
                          f"({skill['successes']}/{skill['attempts']} past successes).")
        except Exception:
            pass

        base_goal = (ctx + "Current task: " + goal) if ctx else goal
        plan = self.llm.plan(base_goal + lessons, self.current_url())
        ptext = _plan_text(plan)
        self._log(f"Plan:\n{ptext}")

        hist, stall, replans, goal_steps = [], 0, 0, 0
        status, result = "running", None
        gathered, consec_reads, no_progress, last_sig = [], 0, 0, None
        last_act_sig, same_act = None, 0
        approved_commit = False   # a fresh request_approval covers the next commit click

        while goal_steps < cfg.MAX_STEPS:
            goal_steps += 1
            self.step += 1
            scan = self.driver.scan()
            ax_path = self.sdir / "ax" / f"step_{self.step}.json"
            ax_path.write_text(json.dumps(scan)[:200000], encoding="utf-8")
            shot_path = self.sdir / "shots" / f"step_{self.step}.png"
            self.driver.screenshot(str(shot_path))

            before = signature(scan)

            # anti-spiral: is the page actually changing between steps?
            no_progress = no_progress + 1 if before == last_sig else 0
            last_sig = before
            if no_progress >= cfg.NO_PROGRESS_LIMIT:
                self._log("   no progress for several steps; stopping with partial result")
                status = "stopped_no_progress"
                tail = "\n---\n".join(gathered)[-1500:]
                result = ("(Stopped — the page stopped changing and I wasn't making "
                          "progress.) Information gathered so far:\n" + tail)
                break

            ginfo = "\n---\n".join(gathered)[-cfg.GATHERED_CAP:] or "(none yet)"
            nudge = ""
            if consec_reads >= cfg.READ_NUDGE_AT:
                nudge += ("\nNote: you have read the page several times — the gathered "
                          "information above is almost certainly enough. Take a new "
                          "action or call done; do not read the same page again.")
            if no_progress >= cfg.NO_PROGRESS_NUDGE:
                nudge += ("\nNote: the page has not changed for a few steps, so you are "
                          "not making progress. Change approach or finish now with done.")

            content = (
                ctx
                + f"GOAL:\n{goal}\n\nPLAN:\n{ptext}\n\n"
                f"INFORMATION GATHERED:\n{ginfo}\n\n"
                f"RECENT ACTIONS:\n{_history_text(hist)}\n\n"
                f"CURRENT PAGE:\n{render_observation(scan)}\n"
                f"{nudge}\n\nChoose the next single action toward the goal."
            )
            escalate = stall >= cfg.ESCALATE_AT or no_progress >= cfg.ESCALATE_AT
            act = self.llm.choose_action(content, escalate=escalate)
            name, args = act["name"], act.get("input") or {}
            tag = "  (escalated -> Opus)" if escalate else ""
            self._log(f"[step {self.step}] {name} {json.dumps(args)}{tag}")

            # stop a spiral: the same action repeated with no effect gets nowhere
            act_sig = f"{name}:{json.dumps(args, sort_keys=True)}"
            same_act = same_act + 1 if act_sig == last_act_sig else 0
            last_act_sig = act_sig
            if (same_act >= cfg.REPEAT_ACTION_LIMIT
                    and name not in ("ask_human", "request_approval", "done")):
                self._log("   same action repeated with no effect; stopping.")
                status = "stopped_repeat"
                result = result or (
                    "(Stopped — I kept repeating the same action with no change. "
                    "Information gathered so far:\n"
                    + ("\n---\n".join(gathered)[-1500:] or "(none)"))
                break

            entry = {"step": self.step, "action": name, "args": redact(args),
                     "result": "", "verified": None, "vreason": "", "read_text": None,
                     # accessible name of the target element, for procedural memory:
                     # element_ids are per-page, so the recipe generalizes to the name.
                     "target": (_element_name(scan, args.get("element_id"))
                                if name in ("click", "type", "select") else None)}

            if name == "done":
                result = args.get("result", "")
                entry["result"], entry["verified"] = "DONE", True
                hist.append(entry)
                self.mem.log_step(self.session_id, self.step, before["url"], name,
                                  redact(args), act, True, "", str(shot_path), str(ax_path))
                status = "success"
                break

            if name == "ask_human":
                q = args.get("question", "(no question)")
                self._log(f"[ask_human] {q}")
                ans = _scrub(self._ask_fn(q), self._log)
                entry["result"], entry["verified"] = f"human: {ans}", True
                hist.append(entry)
                self.mem.log_step(self.session_id, self.step, before["url"], name,
                                  redact(args), act, True, ans, str(shot_path), str(ax_path))
                stall, consec_reads = 0, 0
                continue

            if name == "request_approval":
                ok = self._require_approval(args.get("summary", "(no summary)"),
                                            args.get("details", ""))
                if ok:
                    approved_commit = True   # don't re-prompt on the click that follows
                entry["result"] = "approved" if ok else "declined"
                entry["verified"] = True
                hist.append(entry)
                self.mem.log_step(self.session_id, self.step, before["url"], name,
                                  redact(args), act, True, entry["result"],
                                  str(shot_path), str(ax_path))
                stall, consec_reads = 0, 0
                continue

            if name == "read":
                txt = self.driver.read(args.get("element_id"))
                entry["result"] = f"read {len(txt)} chars"
                entry["read_text"], entry["verified"] = txt, True
                hist.append(entry)
                self.mem.log_step(self.session_id, self.step, before["url"], name,
                                  redact(args), act, True, "", str(shot_path), str(ax_path))
                # keep read text available across the history window (FR-MEM-1)
                gathered.append(txt)
                while sum(len(g) for g in gathered) > cfg.GATHERED_CAP and len(gathered) > 1:
                    gathered.pop(0)
                consec_reads += 1
                stall = 0
                continue

            # mutating action: execute, then verify the ones with a real expected
            # state change. scroll/wait_for often produce no signature delta, so
            # verifying them yields false failures — skip (FR-ACT-3 in spirit).
            consec_reads = 0

            # Login is a human step: never type into a password field — hand off
            # so the human signs in themselves in the browser window (FR-SEC-1).
            if name == "type" and _element(scan, args.get("element_id")).get("role") == "password":
                self._log("   refusing to type into a password field — please sign "
                          "in yourself in the browser window.")
                ans = _scrub(self._ask_fn(
                    "That's a password field. Please sign in yourself in the browser "
                    "window (never type your password here), then reply 'continue'."),
                    self._log)
                entry["result"], entry["verified"] = f"login handoff: {ans}", True
                hist.append(entry)
                self.mem.log_step(self.session_id, self.step, before["url"], name,
                                  {"element_id": args.get("element_id")}, act, True,
                                  "login handoff", str(shot_path), str(ax_path))
                stall = 0
                continue

            # Non-LLM safety net: a click on a control that looks like it commits
            # something irreversible (Send/Submit/Buy/Delete...) is stopped for
            # approval even if the model didn't call request_approval itself.
            committed, commit_name = False, ""
            if name == "click":
                elname = _element_name(scan, args.get("element_id"))
                if _looks_irreversible(elname):
                    committed, commit_name = True, elname
                    if approved_commit:
                        approved_commit = False   # already approved a moment ago
                        self._log(f"   proceeding — '{elname}' already approved.")
                    elif not self._require_approval(
                            f"click \"{elname}\" on {before['url']} — this looks "
                            "irreversible (send/submit/buy/delete)."):
                        entry["result"] = f"blocked: user declined '{elname}'"
                        entry["verified"] = False
                        entry["vreason"] = "approval declined"
                        hist.append(entry)
                        self.mem.log_step(self.session_id, self.step, before["url"],
                                          name, redact(args), act, False,
                                          "approval declined", str(shot_path), str(ax_path))
                        stall = 0
                        continue

            res = self.driver.execute(name, args)
            if name in ("scroll", "wait_for"):
                verified, vnote = res["ok"], "not verified (low-risk action)"
            elif name == "navigate":
                # a navigation that landed on a rendered page is success; no need
                # to ask the verifier (which false-failed on slow-rendering SPAs).
                after = signature(self.driver.scan())
                verified = res["ok"] and after.get("count", 0) > 0
                vnote = "navigated" if verified else "page did not render"
            else:
                after = signature(self.driver.scan())
                v = self.llm.verify(f"{name} {json.dumps(args)}", before, after)
                verified = bool(v.get("satisfied")) and res["ok"]
                vnote = v.get("reason", "")
            entry["result"] = res["detail"]
            entry["verified"] = verified
            entry["vreason"] = vnote
            hist.append(entry)
            self.mem.log_step(self.session_id, self.step, before["url"], name,
                              redact(args), act, verified, vnote, str(shot_path), str(ax_path))

            # An approved irreversible click that visibly changed the page took
            # effect (e.g. the compose window closed after Send) — the task is
            # done. Stop here so it can't re-click and re-prompt endlessly.
            if committed and res["ok"] and (
                    after.get("content_hash") != before.get("content_hash")
                    or after.get("count") != before.get("count")
                    or after.get("url") != before.get("url")):
                self._log(f"   '{commit_name}' completed and the page changed — done.")
                status = "success"
                result = result or f"Done — '{commit_name}' was approved and completed."
                break

            if verified:
                stall = 0
                continue

            # recovery ladder
            stall += 1
            self._log(f"   verify failed ({vnote}); stall={stall}")
            if stall == 1:
                self.driver.wait_briefly()  # most failures are timing/races (§9)
            elif stall >= cfg.REPLAN_AT:
                if replans < cfg.MAX_REPLANS:
                    replans += 1
                    stall = 0
                    plan = self.llm.plan(
                        f"{goal}\n[revised attempt {replans}; progress so far:\n"
                        f"{_history_text(hist)}]" + lessons, self.current_url())
                    ptext = _plan_text(plan)
                    self._log(f"   re-planned (#{replans}):\n{ptext}")
                else:
                    self._log("   re-plans exhausted; handing off to human")
                    ans = _scrub(self._ask_fn("I'm stuck on this task. How should I proceed?"),
                                 self._log)
                    hist.append({"step": self.step, "action": "ask_human",
                                 "args": {"question": "stuck"}, "result": f"human: {ans}",
                                 "verified": True, "vreason": "", "read_text": None})
                    stall = 0
        else:
            status = "stopped_step_cap"

        if status == "running":
            status = "stopped"

        # Learning layer: fold this run into procedural memory. On success the
        # winning path is stored (kept if it's a new shortest); either way the
        # verify-failure lessons accumulate. Best-effort — never break the task.
        try:
            recipe, notes = _distill(hist)
            self.mem.learn_skill(intent, site, status, goal_steps, recipe, notes)
        except Exception:
            pass

        result = (result or "") + approval_note
        self.transcript.append({"goal": goal, "result": result or f"({status})"})
        self.last_steps, self.last_replans = goal_steps, replans
        return result, status

    def close(self):
        try:
            self.mem.end_session(self.session_id, "ended", None)
        except Exception:
            pass
        self.driver.close()


def run(goal, start_url=None, headless=True, profile=None, channel=None, cdp_url=None):
    """One-shot entry point used by run.py — builds an Agent, runs once, closes."""
    agent = Agent(headless=headless, start_url=start_url, profile=profile,
                  channel=channel, cdp_url=cdp_url)
    try:
        print(f"\n=== Session {agent.session_id} ===\nGoal: {goal}")
        result, status = agent.run_goal(goal)
        print("\n=== Result ===")
        print(f"Status: {status}")
        if result:
            print(f"\n{result}\n")
        print(f"Steps: {agent.last_steps}   Re-plans: {agent.last_replans}")
        print(f"Estimated cost: ${agent.cost():.4f}")
        for m, u in agent.llm.usage.items():
            print(f"  {m}: in={u['in']} out={u['out']} cache_read={u['cache_read']}")
        print(f"Episodic log: {cfg.SESSIONS_ROOT / 'episodic.db'}  (session {agent.session_id})")
        print(f"Artifacts:    {agent.sdir}")
        return {"session_id": agent.session_id, "status": status, "result": result}
    finally:
        agent.close()
