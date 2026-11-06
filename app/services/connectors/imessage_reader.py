"""Read texts out of the Messages database. Shared by the on-Mac connector
(imessage.py) and the helper that hosted seats use (imessage_helper.py).

The helper ships this file to a Mac that has none of Sparrow installed
(imessage_relay.build_helper), on whatever Python Apple provides, which can be
as old as 3.9. So: standard library plus redact and privacy_class, nothing else.
The filters run here so that what they drop never leaves the Mac.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from app.services import privacy_class as pc
from app.services import redact

# Messages stores dates as nanoseconds since 2001-01-01 (seconds before High
# Sierra, which a pre-2017 row can still carry).
MAC_EPOCH = 978307200
_NS_THRESHOLD = 10 ** 11
TITLE_MAX = 240
BACKFILL_DAYS = 7.0

ACCESS_HINT = (
    "Sparrow can't read your Messages database. On this Mac open System "
    "Settings → Privacy & Security → Full Disk Access, turn on Terminal (or "
    "Sparrow, if you started it from the app), restart it, then try again."
)

_OTP_WORD = re.compile(
    r"(?i)\b(?:code|passcode|password|otp|pin|verification|verify|2fa)\b")
_OTP_DIGITS = re.compile(r"\b\d{4,8}\b")
_BODY_STRING = re.compile(rb"NS(?:Mutable)?String.{1,12}?\+", re.DOTALL)


class AccessError(RuntimeError):
    """The Messages database could not be opened."""


def db_path() -> Path:
    raw = (os.environ.get("QUILL_IMESSAGE_DB") or "").strip()
    return Path(raw) if raw else Path.home() / "Library" / "Messages" / "chat.db"


def contacts_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "AddressBook"


def open_ro(path: Path) -> sqlite3.Connection:
    # mode=ro, not immutable=1: Messages keeps recent rows in the WAL, which
    # immutable ignores.
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def open_messages() -> sqlite3.Connection:
    try:
        conn = open_ro(db_path())
        conn.execute("SELECT 1 FROM message LIMIT 1").fetchone()
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise AccessError(ACCESS_HINT) from exc
    return conn


def to_unix(mac_date) -> float:
    try:
        d = float(mac_date or 0)
    except (TypeError, ValueError):
        return 0.0
    return (d / 1e9 if abs(d) > _NS_THRESHOLD else d) + MAC_EPOCH


def to_mac_ns(ts: float) -> int:
    return int((ts - MAC_EPOCH) * 1e9)


def decode_attributed_body(blob) -> str:
    """The plain text inside an ``attributedBody`` blob.

    Since Ventura ``message.text`` is often NULL and the words live only in
    this typedstream archive. The string follows the NSString class marker as
    ``+``, a length, then UTF-8: lengths of 128+ are flagged by a leading
    0x81 (two bytes follow) or 0x82 (four bytes follow), little-endian.
    """
    if not blob:
        return ""
    hit = _BODY_STRING.search(blob)
    if not hit:
        return ""
    i = hit.end()
    if i >= len(blob):
        return ""
    lead = blob[i]
    if lead == 0x81:
        size, i = int.from_bytes(blob[i + 1:i + 3], "little"), i + 3
    elif lead == 0x82:
        size, i = int.from_bytes(blob[i + 1:i + 5], "little"), i + 5
    else:
        size, i = lead, i + 1
    if size <= 0 or i + size > len(blob):
        return ""
    return blob[i:i + size].decode("utf-8", errors="replace")


def _message_text(row: sqlite3.Row) -> str:
    text = row["text"] if row["text"] else ""
    if not text and "attributedBody" in row.keys():
        text = decode_attributed_body(row["attributedBody"])
    # U+FFFC is the placeholder an attachment leaves in the text.
    return text.replace("￼", "").strip()


def is_person_handle(handle: str) -> bool:
    """A phone number or email. Short codes and sender ids (72345, AMAZON) are
    businesses: bank alerts, deliveries, marketing, not someone you talk to."""
    h = (handle or "").strip()
    if "@" in h:
        return True
    digits = re.sub(r"[\s\-().+]", "", h)
    return digits.isdigit() and len(digits) >= 8


def keep_out(text: str) -> bool:
    """One-time codes and anything secret-shaped never land. Biased to drop:
    losing "door code is 4821" from memory is the right failure."""
    if len(text) < 240 and _OTP_WORD.search(text) and _OTP_DIGITS.search(text):
        return True
    return pc.looks_like_secret(text) or bool(redact.scan(text))


def _handle_key(handle: str) -> str:
    h = (handle or "").strip().lower()
    if "@" in h:
        return h
    return re.sub(r"\D", "", h)[-10:]


def contact_names() -> dict:
    """Phone/email → name from the local Contacts databases. Best effort: a
    failure only means handles stay unnamed."""
    root = contacts_root()
    names: dict = {}
    try:
        paths = [root / "AddressBook-v22.abcddb",
                 *root.glob("Sources/*/AddressBook-v22.abcddb")]
    except OSError:
        return names
    queries = ("SELECT ZFULLNUMBER v, ZOWNER o FROM ZABCDPHONENUMBER",
               "SELECT ZADDRESS v, ZOWNER o FROM ZABCDEMAILADDRESS")
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            conn = open_ro(path)
            try:
                owners = {}
                for r in conn.execute(
                        "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION "
                        "FROM ZABCDRECORD"):
                    person = " ".join(x for x in (r["ZFIRSTNAME"], r["ZLASTNAME"]) if x)
                    owners[r["Z_PK"]] = person or r["ZORGANIZATION"] or ""
                for q in queries:
                    for r in conn.execute(q):
                        name, key = owners.get(r["o"]), _handle_key(r["v"] or "")
                        if name and key:
                            names.setdefault(key, name)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            print(f"[imessage] contacts skipped ({exc}).")
    return names


def fetch_batch(cursor: dict | None, now: float, limit: int) -> tuple:
    """(items, new cursor, more waiting) for texts after the cursor's rowid,
    oldest first. Raises AccessError when Messages can't be read.

    Dropped rows still advance the cursor, so they aren't re-read forever.
    `floor` is fixed at the first run so Messages in iCloud pulling down old
    history later doesn't land it as new.
    """
    cursor = dict(cursor or {})
    try:
        floor = float(cursor["floor"])
    except (KeyError, TypeError, ValueError):
        days = float(os.environ.get("QUILL_CONNECTOR_BACKFILL_DAYS", BACKFILL_DAYS))
        floor = now - days * 86400
    try:
        after = int(cursor.get("rowid") or 0)
    except (TypeError, ValueError):
        after = 0
    conn = open_messages()
    try:
        rows = _select(conn, after, floor, limit)
        items = _to_items(conn, rows)
    finally:
        conn.close()
    last = int(rows[-1]["rid"]) if rows else after
    return items, {**cursor, "floor": floor, "rowid": last}, len(rows) >= limit


def _select(conn: sqlite3.Connection, after: int, floor: float, limit: int) -> list:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(message)")}
    select = ["m.ROWID AS rid", "m.guid", "m.text", "m.date", "m.is_from_me",
              "m.service", "h.id AS handle"]
    if "attributedBody" in cols:
        select.append("m.attributedBody")
    where = ["m.ROWID > ?", "m.date >= ?"]
    # Tapbacks and "renamed the group" rows are not something anyone said.
    for col in ("associated_message_type", "item_type"):
        if col in cols:
            where.append(f"COALESCE(m.{col}, 0) = 0")
    return conn.execute(
        f"SELECT {', '.join(select)}, "
        "(SELECT chat_id FROM chat_message_join j "
        " WHERE j.message_id = m.ROWID LIMIT 1) AS chat_id "
        "FROM message m LEFT JOIN handle h ON h.ROWID = m.handle_id "
        f"WHERE {' AND '.join(where)} ORDER BY m.ROWID LIMIT ?",
        (after, to_mac_ns(floor), limit)).fetchall()


def _to_items(conn: sqlite3.Connection, rows: list) -> list:
    if not rows:
        return []
    names = contact_names()
    chats: dict = {}
    items = []
    for row in rows:
        item = _item(row, _chat(conn, row["chat_id"], chats), names)
        if item:
            items.append(item)
    return items


def _chat(conn: sqlite3.Connection, chat_id, cache: dict) -> dict:
    if chat_id is None:
        return {"guid": None, "name": "", "handles": []}
    if chat_id not in cache:
        c = conn.execute("SELECT guid, display_name FROM chat WHERE ROWID = ?",
                         (chat_id,)).fetchone()
        handles = [r["id"] for r in conn.execute(
            "SELECT h.id FROM chat_handle_join j "
            "JOIN handle h ON h.ROWID = j.handle_id WHERE j.chat_id = ?",
            (chat_id,))]
        cache[chat_id] = {"guid": c["guid"] if c else None,
                          "name": (c["display_name"] or "") if c else "",
                          "handles": handles}
    return cache[chat_id]


def _item(row: sqlite3.Row, chat: dict, names: dict):
    text = _message_text(row)
    if not text or keep_out(text):
        return None
    handle = row["handle"] or ""
    from_self = bool(row["is_from_me"])
    handles = list(chat["handles"])
    if handle and handle not in handles:
        handles.append(handle)
    if from_self:
        if handles and not any(is_person_handle(h) for h in handles):
            return None
    elif not is_person_handle(handle):
        return None

    def show(h: str) -> str:
        return names.get(_handle_key(h)) or h

    # Only names reach `people`: a bare number would mint a junk candidate.
    people = []
    for h in ([handle] if handle and not from_self else []) + handles:
        name = names.get(_handle_key(h))
        if name and name not in people:
            people.append(name)
    others = ", ".join(dict.fromkeys(show(h) for h in handles)) or "unknown"
    where = f" in {chat['name']}" if chat["name"] else ""
    lead = f"To {others}" if from_self else f"From {show(handle)}"
    body = f"{lead}{where}: {text}"
    return {
        "kind": "message", "external_id": row["guid"], "thread_id": chat["guid"],
        # Chat memory lines render the summary, which land_item builds from
        # the title, so the title has to carry the words.
        "ts": to_unix(row["date"]), "title": body[:TITLE_MAX], "body": body,
        "people": people, "direction": "sent" if from_self else "received",
        "from_self": from_self,
        "from": "me" if from_self else handle,
        "to": ", ".join(handles) if from_self else "me",
        "meta": {"privacy_class": pc.SENSITIVE,
                 "service": row["service"] or "iMessage",
                 "group": len(handles) > 1},
    }
