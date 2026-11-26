"""Handler discovery.

A handler is one Python module that defines at least `KIND` and `async def run(task, ctx)`.
Handlers are found, in this order:

1. The built-in modules in this package (`fleetq/handlers/*.py`).
2. Every directory in `FLEETQ_HANDLERS` (colon-separated) and every `--handlers DIR` on the
   command line: each `*.py` file there is imported by its file name.
3. Installed packages that register an entry point in the `fleetq.handlers` group, so a handler
   can ship as its own pip package:

       [project.entry-points."fleetq.handlers"]
       translate = "my_package.translate"

The last definition of a KIND wins, so a project directory can override a built-in handler.
See docs/handlers.md for the handler contract.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import os
import pkgutil
import sys
import traceback

log = logging.getLogger("fleetq.handlers")

ENTRY_POINT_GROUP = "fleetq.handlers"


def handler_dirs(extra: list[str] | None = None) -> list[str]:
    """Directories to scan: FLEETQ_HANDLERS plus whatever the caller adds, absolute, in order."""
    dirs = [d for d in os.environ.get("FLEETQ_HANDLERS", "").split(os.pathsep) if d.strip()]
    dirs += list(extra or [])
    out = []
    for d in dirs:
        d = os.path.abspath(os.path.expanduser(d))
        if d not in out:
            out.append(d)
    return out


def module_names(extra_dirs: list[str] | None = None) -> list[str]:
    """Importable names of every candidate handler module (built-ins, directories, entry points)."""
    names = [f"{__name__}.{m.name}" for m in pkgutil.iter_modules(__path__) if not m.name.startswith("_")]
    for d in handler_dirs(extra_dirs):
        if not os.path.isdir(d):
            log.warning("handler directory %s does not exist", d)
            continue
        if d not in sys.path:
            sys.path.insert(0, d)
        names += [m.name for m in pkgutil.iter_modules([d]) if not m.name.startswith("_")]
    try:
        for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
            names.append(ep.value.split(":", 1)[0])
    except Exception as e:  # noqa: BLE001  (a broken distribution must not take the worker down)
        log.warning("entry point scan failed: %s", e)
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def discover(extra_dirs: list[str] | None = None, reload: bool = False, errors: list[str] | None = None) -> dict[str, object]:
    """KIND -> module for every importable handler. Import errors are logged (and appended to
    `errors` when given) and never raise, so one broken handler does not stop the others."""
    found: dict[str, object] = {}
    for name in module_names(extra_dirs):
        try:
            mod = sys.modules.get(name)
            mod = importlib.reload(mod) if (mod and reload) else (mod or importlib.import_module(name))
        except Exception:  # noqa: BLE001
            tb = traceback.format_exc()
            log.error("failed to import handler module %s:\n%s", name, tb)
            if errors is not None:
                errors.append(f"{name}: {tb.strip().splitlines()[-1]}")
            continue
        kind = getattr(mod, "KIND", None)
        if not kind or not callable(getattr(mod, "run", None)):
            continue
        found[kind] = mod
    return found


def setup_ok(mod) -> tuple[bool, str]:
    """Run a handler's SETUP['check'] if it has one. Never raises."""
    fn = (getattr(mod, "SETUP", None) or {}).get("check")
    if fn is None:
        return True, "no check defined"
    try:
        ok, detail = fn()
        return bool(ok), str(detail)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def capability_tags(mods: dict[str, object]) -> list[str]:
    """Tags this machine earns from handlers that declare `TAG` and whose setup check passes.
    A handler whose NEEDS includes its own TAG (render needs "browser", ocr_page needs "ocr") is
    thereby only ever claimed by workers that can actually run it."""
    tags = []
    for mod in mods.values():
        tag = getattr(mod, "TAG", None)
        if tag and tag not in tags and setup_ok(mod)[0]:
            tags.append(tag)
    return sorted(tags)
