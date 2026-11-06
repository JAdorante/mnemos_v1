"""Shared helpers for the two-Sparrow interaction test (see README.md)."""
from __future__ import annotations

import http.cookiejar
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENV_FILE = REPO / "deploy" / "hosted" / "testco" / ".env"
DEFAULT_OUT = REPO / "data" / "two_sparrow"


def read_env(path: Path = ENV_FILE) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


class FlowLog:
    """One JSONL row per observed event, every row carrying run_id."""

    def __init__(self, path: Path, run_id: str):
        self.path, self.run_id = path, run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, kind: str, **fields) -> dict:
        row = {"run_id": self.run_id, "ts": time.time(), "kind": kind, **fields}
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        return row

    def rows(self) -> list[dict]:
        if not self.path.is_file():
            return []
        return [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]


class Seat:
    """One Sparrow seat. Bearer calls by default; `live=True` uses the
    browser-style session (cookie + CSRF header, no Authorization) that the
    records routes require for a human approval."""

    def __init__(self, name: str, base: str, token: str, container: str, log: FlowLog | None = None):
        self.name, self.base, self.token, self.container, self.log = name, base.rstrip("/"), token, container, log
        self.jar = http.cookiejar.CookieJar()
        self.csrf = ""
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    # -- transport --------------------------------------------------------------
    def call(self, method: str, path: str, body=None, *, live: bool = False,
             timeout: float = 120.0, form: dict | None = None) -> dict:
        url = self.base + path
        headers = {"Accept": "application/json"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if live:
            if not self.csrf:
                self.unlock()
            headers["X-CSRF-Token"] = self.csrf
            headers["Sec-Fetch-Site"] = "same-origin"
        else:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            raise HttpError(e.code, raw) from None
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {"_raw": raw}

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body if body is not None else {}, **kw)

    def unlock(self) -> None:
        """Mint the browser session + CSRF cookie (POST /auth/unlock)."""
        req = urllib.request.Request(self.base + "/auth/unlock", data=json.dumps({"token": self.token}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        with self._opener.open(req, timeout=30) as r:
            out = json.loads(r.read().decode())
        self.csrf = out.get("csrf_token") or ""
        for c in self.jar:
            if c.name == "quill_csrf":
                self.csrf = c.value

    def wait_healthy(self, timeout_s: float = 300.0) -> None:
        t0 = time.time()
        last = ""
        while time.time() - t0 < timeout_s:
            try:
                self.get("/health", timeout=10)
                return
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                time.sleep(3)
        raise RuntimeError(f"{self.name} not healthy after {timeout_s:.0f}s: {last}")

    # -- chat ----------------------------------------------------------------------
    def chat(self, message: str, *, timeout_s: float = 600.0, task: str = "", session_new: bool = False) -> dict:
        """Send one turn and wait until the worker finishes it. Returns the
        events that arrived, the final text, and the time taken."""
        if session_new:
            try:
                self.post("/chat/new")
            except HttpError:
                pass
        before = self.get("/chat/poll?since=0")
        last_seq = max([int(e.get("id") or e.get("seq") or 0) for e in before.get("events") or []] + [0])
        t0 = time.time()
        sent = self.post("/chat", {"message": message})
        if self.log:
            self.log.write("chat.sent", seat=self.name, task=task, message=message, resp=sent)
        seen: list[dict] = []
        final = None
        deadline = t0 + timeout_s
        quiet_since = None
        while time.time() < deadline:
            time.sleep(2)
            poll = self.get(f"/chat/poll?since={last_seq + 1}")
            evs = [e for e in (poll.get("events") or []) if int(e.get("id") or e.get("seq") or 0) > last_seq]
            for e in evs:
                last_seq = max(last_seq, int(e.get("id") or e.get("seq") or 0))
                seen.append(e)
                if e.get("kind") in ("result", "error", "ask"):
                    final = e
            st = poll.get("state") or {}
            busy = bool(st.get("job_id")) or bool(st.get("busy")) or bool((st.get("web") or {}).get("busy"))
            if final is not None and not busy:
                # give trailing system lines a moment, then stop
                if quiet_since is None:
                    quiet_since = time.time()
                elif time.time() - quiet_since > 4:
                    break
            elif final is None and not busy and evs == [] and time.time() - t0 > 30:
                # nothing running and nothing arriving
                if quiet_since is None:
                    quiet_since = time.time()
                elif time.time() - quiet_since > 20:
                    break
            else:
                quiet_since = None
        out = {"seat": self.name, "task": task, "message": message, "events": seen,
               "final_kind": (final or {}).get("kind"), "final_text": (final or {}).get("text"),
               "state": st if 'st' in dir() else {}, "elapsed_s": round(time.time() - t0, 1),
               "timed_out": time.time() >= deadline}
        if self.log:
            self.log.write("chat.done", **{k: v for k, v in out.items() if k != "events"},
                           n_events=len(seen), texts=[(e.get("kind"), (e.get("text") or "")[:2000]) for e in seen])
        return out

    def team_ask(self, question: str, peer_id: str, *, task: str = "", slug: str = "testco",
                 timeout_s: float = 600.0) -> dict:
        """The user clicking the 'Ask the Testco team' chip under their message:
        POST /chat/suggestions/apply with the peer.team_ask payload (what
        parse_team_ask yields on the seat), then wait for the round trip to
        reach a terminal status in /peer/answers. The chat agent never
        dispatches a team ask on its own (see README)."""
        before = {x.get("ask_id") for x in (self.get("/peer/answers").get("answers") or [])}
        t0 = time.time()
        payload = {"fanout": True, "team_slug": slug, "team_name": slug, "peer_ids": [peer_id],
                   "peer_name": f"{slug} team", "question": question, "kind": "question"}
        try:
            applied = self.post("/chat/suggestions/apply", {"action": "peer.team_ask", "payload": payload})
        except HttpError as e:
            applied = {"error": str(e)}
        if self.log:
            self.log.write("chat.sent", seat=self.name, task=task, message=f"[team_ask] {question}", resp=applied)
        final = None
        while time.time() - t0 < timeout_s:
            time.sleep(3)
            rows = [x for x in (self.get("/peer/answers").get("answers") or []) if x.get("ask_id") not in before]
            if rows:
                r = rows[-1]
                if r.get("status") in ("answered", "null", "declined", "policy_denied", "failed", "error", "offline", "expired"):
                    final = r
                    break
        out = {"seat": self.name, "task": task, "message": question, "applied": applied,
               "status": (final or {}).get("status"), "final_kind": "peer_" + str((final or {}).get("status")),
               "final_text": (final or {}).get("answer"), "null_reason": (final or {}).get("null_reason"),
               "hop": (final or {}).get("hop"), "ask_id": (final or {}).get("ask_id"),
               "elapsed_s": round(time.time() - t0, 1), "timed_out": final is None, "events": []}
        if self.log:
            self.log.write("chat.done", **{k: v for k, v in out.items() if k != "events"}, n_events=0,
                           texts=[("peer_answer", (out["final_text"] or "")[:3000])])
        return out

    # -- docker ---------------------------------------------------------------------------
    def data_file(self, rel: str) -> str:
        """Read a file from the seat's data volume (docker exec cat)."""
        import subprocess
        r = subprocess.run(["docker", "exec", self.container, "sh", "-c", f"cat /srv/sparrow/data/{rel} 2>/dev/null"],
                           capture_output=True, text=True, timeout=60)
        return r.stdout


def seats(log: FlowLog | None = None) -> tuple[Seat, Seat]:
    env = read_env()
    a = Seat("alice", os.environ.get("TESTCO_ALICE_URL", "http://127.0.0.1:8007"), env["TESTCO_ALICE_TOKEN"], "testco-alice-1", log)
    b = Seat("bob", os.environ.get("TESTCO_BOB_URL", "http://127.0.0.1:8008"), env["TESTCO_BOB_TOKEN"], "testco-bob-1", log)
    return a, b


def run_dir(run_id: str) -> Path:
    d = DEFAULT_OUT / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state(run_id: str) -> dict:
    p = run_dir(run_id) / "state.json"
    return json.loads(p.read_text()) if p.is_file() else {}


def save_state(run_id: str, state: dict) -> None:
    (run_dir(run_id) / "state.json").write_text(json.dumps(state, indent=1, default=str))
