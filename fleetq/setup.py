"""Per-handler machine setup.

A handler module may declare:

    SETUP = {
        "pip":   ["playwright>=1.47"],                 # installed into this venv with uv (or pip)
        "macos": ["{python} -m playwright install chromium"],
        "linux": ["{python} -m playwright install --with-deps chromium"],
        "any":   ["..."],                              # shell steps for every OS
        "check": setup_check,                          # callable -> (ok: bool, detail: str)
    }

`fleetq setup` runs the check first and skips handlers that already pass, so it is safe
to run on every deploy. `fleetq doctor` only reports.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys


def _os_key() -> str:
    return {"Darwin": "macos", "Linux": "linux"}.get(platform.system(), "other")


def load_all_handlers(handler_dirs: list[str]) -> dict[str, object]:
    """Import every handler module regardless of this machine's tags."""
    from . import handlers as registry

    errors: list[str] = []
    mods = registry.discover(handler_dirs, errors=errors)
    for e in errors:
        print(f"  ! cannot import {e}")
    return mods


def _pip_install(pkgs: list[str]) -> tuple[bool, str]:
    if not pkgs:
        return True, ""
    uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
    if os.path.exists(uv):
        cmd = [uv, "pip", "install", "-q", "--python", sys.executable, *pkgs]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-q", *pkgs]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[-800:]


def _shell(cmds: list[str]) -> tuple[bool, str]:
    for c in cmds:
        c = c.format(python=sys.executable, home=os.path.expanduser("~"))
        print(f"    $ {c}")
        r = subprocess.run(c, shell=True, text=True)
        if r.returncode != 0:
            return False, f"exit {r.returncode}: {c}"
    return True, ""


def _check(mod) -> tuple[bool, str]:
    fn = (getattr(mod, "SETUP", {}) or {}).get("check")
    if fn is None:
        return True, "no check defined"
    try:
        ok, detail = fn()
        return bool(ok), detail
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def run_setup(kinds: list[str] | None, handler_dirs: list[str], check_only: bool, force: bool) -> int:
    mods = load_all_handlers(handler_dirs)
    wanted = {k: m for k, m in mods.items() if not kinds or k in kinds}
    missing = set(kinds or []) - set(mods)
    for k in sorted(missing):
        print(f"  ! unknown handler kind {k!r}")
    oskey = _os_key()
    print(f"host {platform.node()} ({oskey}, {platform.machine()}, python {sys.executable})")
    failures = 0
    for kind, mod in sorted(wanted.items()):
        setup = getattr(mod, "SETUP", {}) or {}
        ok, detail = _check(mod)
        if check_only:
            print(f"  {'ok ' if ok else 'MISSING'}  {kind:<10} {detail}")
            failures += 0 if ok else 1
            continue
        if ok and not force:
            print(f"  ok       {kind:<10} {detail}")
            continue
        print(f"  setup    {kind:<10} {detail}")
        ok, err = _pip_install(setup.get("pip", []))
        if ok:
            ok, err = _shell(setup.get("any", []) + setup.get(oskey, []))
        if ok:
            ok, detail = _check(mod)
            err = "" if ok else f"check still failing: {detail}"
        if ok:
            print(f"  done     {kind:<10} {detail}")
        else:
            failures += 1
            print(f"  FAILED   {kind:<10} {err}")
    return 1 if failures else 0
