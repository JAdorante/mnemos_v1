#!/usr/bin/env python
"""Restore a Sparrow backup zip into a data directory (WS-B).

Offline and deliberate: this is the "my disk died" path, not a button in the
app. In-app restore is out of scope for the Sept 8 pilot — a half-restored
data directory under a running server is a much worse failure than a manual
step.

    python scripts/restore_backup.py mnemos-backup-20250908-141200.zip data

What it does, in order:

1. **Refuses while Sparrow is running.** Probes ``/health`` on the configured
   host/port; a live server would keep writing into the directory being
   swapped out. ``--force`` overrides (you are on your own).
2. **Validates the manifest** — kind, backup schema version, and the app
   version the backup came from — before touching anything.
3. **Extracts to a temp directory beside the target**, then swaps atomically:
   the existing directory is renamed aside, the new one moved into place, and
   the old one is only removed once the swap succeeded. An interrupted restore
   leaves either the old directory or the old directory plus a temp — never a
   half-written one.

Exit codes: 0 restored, 1 refused (server up / bad manifest / bad zip), 2 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MANIFEST_NAME = "manifest.json"
SUPPORTED_SCHEMAS = (1,)


def server_is_up(host: str, port: int, timeout: float = 1.5) -> bool:
    """True when something answers /health — i.e. Sparrow is probably running."""
    from urllib.error import URLError
    from urllib.request import urlopen
    try:
        with urlopen(f"http://{host}:{port}/health", timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 200)) < 500
    except URLError:
        return False
    except Exception:
        return False


def read_manifest(zf: zipfile.ZipFile) -> dict[str, Any]:
    try:
        return json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
    except KeyError as exc:
        raise ValueError("no manifest.json at the zip root — not a Sparrow "
                         "backup (a takeout export cannot be restored)") from exc
    except Exception as exc:
        raise ValueError(f"unreadable manifest.json: {exc}") from exc


def validate(manifest: dict[str, Any], *, current_version: str,
             strict: bool = True) -> list[str]:
    """Return a list of problems; empty means safe to restore."""
    problems = []
    if manifest.get("kind") != "mnemos.backup":
        problems.append(f"not a Sparrow backup (kind={manifest.get('kind')!r}); "
                        "takeout exports are portable copies, not restorable")
    schema = manifest.get("schema")
    if schema not in SUPPORTED_SCHEMAS:
        problems.append(f"backup schema {schema!r} is not supported by this "
                        f"build (supports {SUPPORTED_SCHEMAS})")
    if strict:
        from packaging.version import InvalidVersion, Version
        made_with = manifest.get("app_version")
        try:
            if made_with and Version(str(made_with)) > Version(current_version):
                problems.append(
                    f"backup was made by Sparrow {made_with}, newer than this "
                    f"build ({current_version}) — install the newer build "
                    "first, or re-run with --force")
        except InvalidVersion:
            problems.append(f"unreadable app_version {made_with!r}")
    return problems


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Members under `data/`, with traversal and absolute paths refused."""
    out = []
    for info in zf.infolist():
        name = info.filename
        if name == MANIFEST_NAME or name.endswith("/"):
            continue
        if not name.startswith("data/"):
            continue
        rel = Path(name[len("data/"):])
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"refusing unsafe path in zip: {name}")
        out.append(info)
    return out


def restore(zip_path: Path, data_dir: Path, *, force: bool = False,
            keep_old: bool = False) -> dict[str, Any]:
    from app.version import __version__

    if not zip_path.is_file():
        raise ValueError(f"no such backup: {zip_path}")
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            raise ValueError(f"backup zip is corrupt at {bad}")
        manifest = read_manifest(zf)
        problems = validate(manifest, current_version=__version__,
                            strict=not force)
        if problems:
            raise ValueError("; ".join(problems))
        members = _safe_members(zf)
        if not members:
            raise ValueError("backup contains no data/ entries")

        data_dir = data_dir.resolve()
        data_dir.parent.mkdir(parents=True, exist_ok=True)
        # Stage beside the target so the final move is a rename on one device.
        staging = Path(tempfile.mkdtemp(prefix=".mnemos-restore-",
                                        dir=str(data_dir.parent)))
        try:
            for info in members:
                rel = info.filename[len("data/"):]
                dest = staging / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
            (staging / "RESTORED_FROM.json").write_text(
                json.dumps({"backup": zip_path.name, "manifest": manifest,
                            "restored_at": time.time()}, indent=2),
                encoding="utf-8")

            aside = None
            if data_dir.exists():
                aside = data_dir.with_name(
                    f"{data_dir.name}.pre-restore-{int(time.time())}")
                os.rename(data_dir, aside)
            try:
                os.rename(staging, data_dir)
            except OSError:
                if aside is not None:      # put the original back, then fail
                    os.rename(aside, data_dir)
                raise
            staging = None
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

    if aside is not None and not keep_old:
        shutil.rmtree(aside, ignore_errors=True)
        aside = None
    return {"ok": True, "data_dir": str(data_dir), "files": len(members),
            "manifest": manifest,
            "previous_kept_at": str(aside) if aside else None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zip", type=Path, help="backup zip from POST /export/backup")
    ap.add_argument("data_dir", type=Path, nargs="?", default=Path("data"),
                    help="target data directory (default: data)")
    ap.add_argument("--force", action="store_true",
                    help="skip the running-server probe and version checks")
    ap.add_argument("--keep-old", action="store_true",
                    help="keep the replaced directory as data.pre-restore-<ts>")
    ap.add_argument("--host", default=os.environ.get("QUILL_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("QUILL_PORT", "8000")))
    args = ap.parse_args(argv)

    if not args.force and server_is_up(args.host, args.port):
        print(f"Sparrow is still running on {args.host}:{args.port}.\n"
              "Stop it first (close start.bat / the Sparrow window), then re-run.\n"
              "A restore while the server is writing would corrupt the result.",
              file=sys.stderr)
        return 1
    try:
        out = restore(args.zip, args.data_dir, force=args.force,
                      keep_old=args.keep_old)
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        print(f"Restore refused: {exc}", file=sys.stderr)
        return 1
    made = out["manifest"].get("created_at")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(made)) if made else "?"
    print(f"Restored {out['files']} file(s) into {out['data_dir']}")
    print(f"  backup taken {when} by Sparrow {out['manifest'].get('app_version')}")
    counts = out["manifest"].get("counts") or {}
    if counts:
        print("  " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if out["previous_kept_at"]:
        print(f"  previous data kept at {out['previous_kept_at']}")
    print("Start Sparrow again — the timeline and search should be intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
