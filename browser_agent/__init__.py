"""FS-BA-001 P0 — autonomous read-only browser agent (Anthropic-backed)."""

# The agent logs Unicode (→ arrows, box-drawing, curly quotes). Windows consoles
# default to cp1252, where those raise UnicodeEncodeError on print() and kill the
# run. Force UTF-8 on stdout/stderr (with replacement as a last resort) so console
# output is safe on every platform. No-op where the stream can't be reconfigured.
import sys as _sys

for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
