"""Episodic memory (FR-MEM-2): an append-only, replayable audit trail.

P0 uses SQLite for a zero-setup PoC; the schema is deliberately Postgres-
portable (swap the connection in one place to move to Postgres later, per the
recommended stack). Every step records action, redacted args, model used,
verification result, token counts, and references to the screenshot + AX
snapshot on disk (NFR-5: every session replayable).
"""
import json
import sqlite3
import time
from pathlib import Path

# Forward-compat with P1: secrets are referenced by handle and a non-LLM
# injector substitutes them (FR-SEC-1), so they should never reach args here.
# This is a belt-and-suspenders redactor in case a secret-named key slips in.
_SECRET_KEYS = ("password", "passwd", "secret", "token", "card", "cvv", "ssn", "otp", "pin")


def redact(args):
    if not isinstance(args, dict):
        return args
    out = {}
    for k, v in args.items():
        out[k] = "***" if any(s in str(k).lower() for s in _SECRET_KEYS) else v
    return out


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Memory:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self._init()

    def _init(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                goal TEXT, plan TEXT,
                started_at TEXT, ended_at TEXT,
                status TEXT, result TEXT
            );
            CREATE TABLE IF NOT EXISTS steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, step INTEGER, ts TEXT,
                url TEXT, action TEXT, args TEXT,
                reasoning TEXT, model TEXT,
                verified INTEGER, verify_note TEXT,
                screenshot_path TEXT, ax_path TEXT,
                input_tokens INTEGER, output_tokens INTEGER
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, ts TEXT, source TEXT,
                input_text TEXT, intent TEXT,
                requires_browser INTEGER, requires_approval INTEGER,
                tool TEXT, site TEXT, rationale TEXT,
                execution_status TEXT
            );
            -- Procedural memory (the learning layer): one distilled "skill" per
            -- (intent, site). Auto-captured after each goal — the winning path
            -- and the failure lessons — and fed back to the planner next time so
            -- runs get shorter, cheaper, and stop repeating mistakes. Compounds
            -- the more the agent is used.
            CREATE TABLE IF NOT EXISTS skills (
                intent TEXT, site TEXT,
                recipe TEXT,            -- JSON: ordered, page-independent winning steps
                failure_notes TEXT,     -- JSON: distinct lessons ("x.com → wait for render")
                successes INTEGER DEFAULT 0,
                attempts INTEGER DEFAULT 0,
                best_steps INTEGER,     -- fewest steps a success ever took
                updated_at TEXT,
                PRIMARY KEY (intent, site)
            );
            """
        )
        self.conn.commit()

    def log_event(self, session_id, input_text, route, source="user_request",
                  status="routed"):
        """The Mnemos event record (PRD data model): one row per user request,
        capturing the router's intent/approval decision before execution."""
        self.conn.execute(
            "INSERT INTO events(session_id, ts, source, input_text, intent, "
            "requires_browser, requires_approval, tool, site, rationale, "
            "execution_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id, _now(), source, input_text, route.get("intent"),
                1 if route.get("requires_browser") else 0,
                1 if route.get("requires_user_approval") else 0,
                route.get("tool"), route.get("site"), route.get("rationale"),
                status,
            ),
        )
        self.conn.commit()

    def start_session(self, session_id, goal, plan):
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions(session_id, goal, plan, started_at, status) "
            "VALUES (?,?,?,?,?)",
            (session_id, goal, json.dumps(plan), _now(), "running"),
        )
        self.conn.commit()

    def log_step(self, session_id, step, url, action, args, act, verified, vnote, shot, ax):
        usage = act.get("usage", {}) if isinstance(act, dict) else {}
        self.conn.execute(
            "INSERT INTO steps(session_id, step, ts, url, action, args, reasoning, model, "
            "verified, verify_note, screenshot_path, ax_path, input_tokens, output_tokens) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id, step, _now(), url, action, json.dumps(args),
                (act.get("reasoning") or "")[:2000], act.get("model"),
                1 if verified else 0, (vnote or "")[:500], shot, ax,
                usage.get("in", 0), usage.get("out", 0),
            ),
        )
        self.conn.commit()

    # --- procedural memory (the learning layer) ----------------------------
    def recall_skill(self, intent, site, max_notes=8):
        """Return the learned skill for this (intent, site), or None. The recipe
        is the shortest successful path seen; failure_notes are the accumulated
        lessons. Fed to the planner so it can reuse the path and dodge mistakes."""
        cur = self.conn.execute(
            "SELECT recipe, failure_notes, successes, attempts, best_steps "
            "FROM skills WHERE intent=? AND site=?",
            (intent or "unknown", site or "web"),
        )
        row = cur.fetchone()
        if not row:
            return None
        recipe = json.loads(row[0] or "[]")
        notes = json.loads(row[1] or "[]")
        return {
            "recipe": recipe,
            "failure_notes": notes[:max_notes],
            "successes": row[2] or 0,
            "attempts": row[3] or 0,
            "best_steps": row[4],
        }

    def learn_skill(self, intent, site, status, steps, recipe, failure_notes,
                    max_notes=12):
        """Fold one finished run into the (intent, site) skill. Success updates
        the recipe only when it's a NEW-shortest path (so the stored playbook
        trends toward fewer steps); failure notes accumulate as a deduped union
        either way. Append-only in spirit: we never lose a lesson."""
        intent, site = (intent or "unknown"), (site or "web")
        success = status == "success"
        prev = self.recall_skill(intent, site, max_notes=10**6)

        notes = list(dict.fromkeys(
            [n for n in ((prev["failure_notes"] if prev else []) + list(failure_notes)) if n]
        ))[:max_notes]

        best = prev["best_steps"] if prev else None
        stored_recipe = prev["recipe"] if prev else []
        if success and recipe and (best is None or steps <= best):
            stored_recipe = recipe          # a new best (or first) winning path
            best = steps if best is None else min(best, steps)

        successes = (prev["successes"] if prev else 0) + (1 if success else 0)
        attempts = (prev["attempts"] if prev else 0) + 1

        self.conn.execute(
            "INSERT OR REPLACE INTO skills(intent, site, recipe, failure_notes, "
            "successes, attempts, best_steps, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (intent, site, json.dumps(stored_recipe), json.dumps(notes),
             successes, attempts, best, _now()),
        )
        self.conn.commit()

    def end_session(self, session_id, status, result):
        self.conn.execute(
            "UPDATE sessions SET ended_at=?, status=?, result=? WHERE session_id=?",
            (_now(), status, result, session_id),
        )
        self.conn.commit()
