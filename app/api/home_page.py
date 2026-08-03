"""RETIRED — classic Today's Intelligence layout (pre–Track E).

No longer routed. Canonical Today is `shell_page.SHELL_PAGE` at GET /today
(GET /shell permanently redirects there).

§2 margin-rail decision: the right-hand "In the margin" ambient notes rail
from this layout is NOT ported onto the new Today dashboard. Ambient margin
notes remain a Memory Console feature (`/memory`). Today's job is the user's
day (attention bands + approvals); the margin is a memory-side reading aid.

Kept on disk for archaeology until a later cleanup deletes the module.
"""

from app.api.vinceo_theme import apply as _vinceo

HOME_PAGE = _vinceo(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@BRAND@@ — Today (retired)</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
<p>This layout is retired. <a href="/today">Open Today</a>.</p>
</body>
</html>
""")
