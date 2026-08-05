"""Site credential store — local env file, never sent to the LLM.

Saved credentials live in `.credentials.env` (gitignored) as:

    QUILL_CRED_GMAIL_COM_USER=user@example.com
    QUILL_CRED_GMAIL_COM_PASS=secret

The agent injects them directly via Playwright (FR-SEC-1: secrets bypass the
model). Users can save with `/save-creds <site> <username> <password>` when
prompted at a login wall, or POST /credentials from the API.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

try:
    from dotenv import load_dotenv
except Exception:  # optional
    load_dotenv = None

_PREFIX = "QUILL_CRED_"
_USER_SUFFIX = "_USER"
_PASS_SUFFIX = "_PASS"
_SAVE_RE = re.compile(
    r"^/?save-creds\s+(\S+)\s+(\S+)\s+(\S+)\s*$", re.I)

# When you save for one host, mirror to related login hosts (same account).
_SITE_ALIASES: dict[str, list[str]] = {
    "gmail.com": ["google.com", "accounts.google.com", "mail.google.com"],
    "google.com": ["accounts.google.com", "mail.google.com", "gmail.com"],
    "accounts.google.com": ["google.com", "mail.google.com", "gmail.com"],
    "outlook.com": ["live.com", "login.live.com", "office.com", "microsoft.com"],
    "live.com": ["outlook.com", "login.live.com", "office.com"],
}


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def credentials_path() -> Path:
    raw = os.environ.get("QUILL_CREDENTIALS_FILE", ".credentials.env")
    p = Path(raw)
    return p if p.is_absolute() else _project_root() / p


def _load_file() -> None:
    path = credentials_path()
    if load_dotenv and path.is_file():
        load_dotenv(path, override=True)


def site_key(site_or_url: str) -> str:
    """Normalize a host or URL to an env key segment, e.g. 'gmail.com' -> 'GMAIL_COM'."""
    host = (site_or_url or "").strip().lower()
    if "://" in host:
        host = urlsplit(host).netloc.lower()
    host = host.split("@")[-1]  # user@host -> host
    host = host.split(":")[0]     # host:port -> host
    if host.startswith("www."):
        host = host[4:]
    slug = re.sub(r"[^a-z0-9]+", "_", host).strip("_").upper()
    return slug or "WEB"


def _user_key(site: str) -> str:
    return f"{_PREFIX}{site_key(site)}{_USER_SUFFIX}"


def _pass_key(site: str) -> str:
    return f"{_PREFIX}{site_key(site)}{_PASS_SUFFIX}"


def host_from_url(url: str) -> str:
    try:
        h = urlsplit(url or "").netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _host_candidates(host: str) -> list[str]:
    """Hosts to try when looking up saved credentials (exact, then registrable domain)."""
    host = (host or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return []
    out = [host]
    parts = host.split(".")
    if len(parts) > 2:
        parent = ".".join(parts[-2:])
        if parent not in out:
            out.append(parent)
    return out


def get(site_or_url: str) -> dict[str, str] | None:
    """Return {site, username, password} for a host, or None."""
    _load_file()
    host = host_from_url(site_or_url) if "://" in (site_or_url or "") else (site_or_url or "")
    if not host:
        return None
    for candidate in _host_candidates(host):
        user = os.environ.get(_user_key(candidate), "").strip()
        pw = os.environ.get(_pass_key(candidate), "").strip()
        if user and pw:
            return {"site": candidate, "username": user, "password": pw}
    return None


def save(site_or_url: str, username: str, password: str) -> dict[str, str]:
    """Upsert credentials for a site into `.credentials.env`. Returns {site, key_user}."""
    host = host_from_url(site_or_url) if "://" in (site_or_url or "") else (site_or_url or "").strip()
    if not host:
        raise ValueError("site/host is required")
    user = (username or "").strip()
    pw = password or ""
    if not user or not pw:
        raise ValueError("username and password are required")
    # Dotenv is line-oriented; newlines/CR/NUL in values become env-var injection.
    for field, value in (("username", user), ("password", pw)):
        if any(c in value for c in ("\n", "\r", "\x00")):
            raise ValueError(f"{field} must be a single line (no newlines)")

    targets = [host] + [a for a in _SITE_ALIASES.get(host, []) if a != host]
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    drop_keys = {_user_key(t) for t in targets} | {_pass_key(t) for t in targets}
    lines: list[str] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if any(line.startswith(f"{k}=") for k in drop_keys):
                continue
            lines.append(line)
    while lines and not lines[-1].strip():
        lines.pop()
    for t in targets:
        uk, pk = _user_key(t), _pass_key(t)
        lines.append(f"{uk}={user}")
        lines.append(f"{pk}={pw}")
        os.environ[uk] = user
        os.environ[pk] = pw
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"site": host, "user_key": _user_key(host), "aliases": targets[1:]}


def try_save_from_reply(reply: str, current_url: str = "") -> dict[str, str] | None:
    """Parse `/save-creds site user pass` from a login-handoff reply."""
    m = _SAVE_RE.match((reply or "").strip())
    if not m:
        return None
    site, user, pw = m.group(1), m.group(2), m.group(3)
    if site.lower() in ("here", "this", "auto") and current_url:
        site = host_from_url(current_url)
    return save(site, user, pw)


def list_sites() -> list[str]:
    """Hosts with stored credentials (password values omitted)."""
    _load_file()
    sites: set[str] = []
    for k in os.environ:
        if k.startswith(_PREFIX) and k.endswith(_USER_SUFFIX):
            slug = k[len(_PREFIX):-len(_USER_SUFFIX)]
            sites.add(slug.lower().replace("_", "."))
    return sorted(sites)


def _find_login_fields(scan: dict | None) -> tuple[dict | None, dict | None]:
    if not scan:
        return None, None
    elements = scan.get("elements") or []
    pw_el = next((e for e in elements if e.get("role") == "password"), None)
    user_el = None
    for e in elements:
        role = (e.get("role") or "").lower()
        name = (e.get("name") or "").lower()
        if role == "password":
            continue
        if not e.get("editable"):
            continue
        if role in ("email", "text", "tel") or any(w in name for w in (
                "email", "user", "login", "account", "phone")):
            user_el = e
            break
    if pw_el and not user_el:
        for e in elements:
            if e.get("editable") and e.get("role") != "password":
                user_el = e
                break
    return user_el, pw_el


def inject_login(driver, scan: dict | None, creds: dict[str, str]) -> bool:
    """Fill username/password via Playwright — never through the LLM. Returns True if filled."""
    user_el, pw_el = _find_login_fields(scan)
    if not user_el or not pw_el:
        return False
    if (pw_el.get("value") or "").startswith("•"):
        return False  # already filled (autofill)
    try:
        driver._loc(user_el["id"]).fill(creds["username"], timeout=8000)
        driver._loc(pw_el["id"]).fill(creds["password"], timeout=8000)
        return True
    except Exception:
        return False
