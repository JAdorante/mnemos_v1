"""Platform UI automation facade — UIA on Windows, AT-SPI on Linux X11."""
from __future__ import annotations

import os

if os.name == "nt":
    from .uia import *  # noqa: F403
    from . import uia as _impl
else:
    from .a11y_linux import *  # noqa: F403
    from . import a11y_linux as _impl

# Driver reads these; `import *` skips leading-underscore names.
_lock = _impl._lock
_last_app = _impl._last_app
_last_controls = _impl._last_controls
