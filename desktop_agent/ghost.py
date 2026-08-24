"""Platform ghost-desktop facade — Win32 on Windows, X11 on Linux."""
from __future__ import annotations

import os

if os.name == "nt":
    from .ghost_win import *  # noqa: F403
else:
    from .ghost_x11 import *  # noqa: F403
