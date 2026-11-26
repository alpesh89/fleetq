"""Load KEY=VALUE pairs from a .env file into os.environ (no dependency).

Real environment variables always win over the file. Lines starting with # are comments; values
may be quoted with ' or "; an optional leading `export ` is ignored.

The file is looked for in the fleet home directory: `FLEETQ_HOME` if set, else the repository
root when fleetq runs from a checkout (the directory holding pyproject.toml), else the current
working directory.
"""

import os

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def home() -> str:
    h = os.environ.get("FLEETQ_HOME", "").strip()
    if h:
        return os.path.abspath(os.path.expanduser(h))
    if os.path.exists(os.path.join(_PKG_PARENT, "pyproject.toml")):
        return _PKG_PARENT
    return os.getcwd()


def load(path: str | None = None) -> dict[str, str]:
    path = path or os.path.join(home(), ".env")
    loaded: dict[str, str] = {}
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return loaded
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = val
            loaded[key] = val
    return loaded
