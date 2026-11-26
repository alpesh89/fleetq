"""Code identity: a short hash of every .py file in the fleetq package.

Workers report it in their heartbeat; the dashboard compares it with the coordinator's copy
and flags any machine running different code. Handler directories passed with --handlers
are included too, so a project-specific handler that lags shows up as well.
"""

from __future__ import annotations

import hashlib
import os

PKG = os.path.dirname(os.path.abspath(__file__))


def code_hash(extra_dirs: list[str] | None = None) -> str:
    h = hashlib.sha256()
    for root_dir in [PKG] + sorted(extra_dirs or []):
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    p = os.path.join(dirpath, fn)
                    h.update(os.path.relpath(p, root_dir).encode())
                    with open(p, "rb") as f:
                        h.update(f.read())
    return h.hexdigest()[:12]
