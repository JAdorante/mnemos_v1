"""Single source of truth for the app version.

Everything that reports a version reads it from here: ``/health``, the Console
footer, the crash-report manifest, the ``usage_daily.version`` column, export
manifests, and the installer stamp (see packaging/). Bump this one constant and
the whole surface moves together — before this existed, nothing carried a
version at all and tester bug reports were unattributable.

Format is semver (``packaging.version`` parses it; see services/update_check.py
for the comparison rules, including prereleases like ``0.4.1rc1``).
"""
from __future__ import annotations

__version__ = "0.4.0"

__all__ = ["__version__"]
