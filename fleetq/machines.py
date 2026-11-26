"""Machine registry for the dashboard: inventory.txt + deploy/roles.txt in the fleet home, plus
~/.ssh/config, plus running the worker installer in the background with a captured log.

The installer rsyncs this checkout to the target host, so the dashboard that installs workers
must run from a git checkout of fleetq (FLEETQ_HOME, or the repository root by default)."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
import time

from . import config

HERE = config.HOME
INVENTORY = os.path.join(HERE, "inventory.txt")
ROLES = os.path.join(HERE, "deploy", "roles.txt")
INSTALLER = os.path.join(HERE, "deploy", "install_worker.sh")
LOGS = os.path.join(HERE, ".deploy-logs")
SSH_CONFIG = os.path.expanduser("~/.ssh/config")
DEFAULT_KEY = os.environ.get("FLEETQ_SSH_KEY", "~/.ssh/fleetq")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR"]

_lock = threading.Lock()
_installs: dict[str, dict] = {}  # alias -> {state, started, finished, log, rc}


def read_inventory() -> list[str]:
    out = []
    try:
        with open(INVENTORY) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    out.append(line)
    except FileNotFoundError:
        pass
    return out


def read_roles() -> dict[str, list[str]]:
    roles = {}
    try:
        with open(ROLES) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    parts = line.split()
                    roles[parts[0]] = parts[1].split(",") if len(parts) > 1 else []
    except FileNotFoundError:
        pass
    return roles


def read_ssh_config() -> dict[str, dict]:
    """Host alias -> {hostname, user, identityfile} from ~/.ssh/config (wildcards skipped)."""
    hosts, cur = {}, None
    try:
        with open(SSH_CONFIG) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"^(\w+)\s+(.*)$", line)
                if not m:
                    continue
                k, v = m.group(1).lower(), m.group(2).strip()
                if k == "host":
                    cur = None if any(ch in v for ch in "*?") else hosts.setdefault(v, {})
                elif cur is not None:
                    cur[k] = v
    except FileNotFoundError:
        pass
    return hosts


def coordinator_ip() -> str:
    """This machine's LAN address (the one a default route would use)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def list_machines() -> list[dict]:
    cfg, roles = read_ssh_config(), read_roles()
    out = []
    for alias in read_inventory():
        c = cfg.get(alias, {})
        inst = _installs.get(alias, {})
        out.append(
            {
                "alias": alias,
                "hostname": c.get("hostname"),
                "user": c.get("user"),
                "kinds": roles.get(alias),
                "in_ssh_config": alias in cfg,
                "install": {k: v for k, v in inst.items() if k != "log"} if inst else None,
            }
        )
    return out


def add_machine(alias: str, hostname: str, user: str, kinds: list[str] | None, key: str | None = None) -> dict:
    alias = alias.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", alias or ""):
        raise ValueError("alias must be letters, digits, - _ .")
    if not hostname.strip() or not user.strip():
        raise ValueError("hostname and user are required")
    cfg = read_ssh_config()
    created_ssh = False
    if alias not in cfg:
        block = f"\nHost {alias}\n\tHostName {hostname.strip()}\n\tUser {user.strip()}\n\tIdentityFile {key or DEFAULT_KEY}\n"
        os.makedirs(os.path.dirname(SSH_CONFIG), exist_ok=True)
        with open(SSH_CONFIG, "a") as f:
            f.write(block)
        created_ssh = True
    if alias not in read_inventory():
        with open(INVENTORY, "a") as f:
            f.write(f"{alias}\n")
    if kinds is not None:
        roles = read_roles()
        roles[alias] = kinds
        lines = []
        try:
            with open(ROLES) as f:
                lines = [ln for ln in f if ln.startswith("#") or ln.strip() == ""]
        except FileNotFoundError:
            lines = ["# alias  kinds\n"]
        with open(ROLES, "w") as f:
            f.writelines(lines)
            for a, ks in roles.items():
                f.write(f"{a:<10}{','.join(ks)}\n")
    return {"alias": alias, "created_ssh_entry": created_ssh}


def probe(alias: str) -> dict:
    """Can we ssh in? What is it?"""
    cmd = [
        "ssh",
        *SSH_OPTS,
        alias,
        'echo "$(uname -s)|$(uname -m)|$(hostname -s)|$USER|$(sudo -n true 2>/dev/null && echo nopass || echo pass)"',
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timed out"}
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        hint = None
        if "Permission denied" in err:
            c = read_ssh_config().get(alias, {})
            hint = f"ssh-copy-id -i {c.get('identityfile', DEFAULT_KEY)}.pub {alias}"
        return {"ok": False, "error": err[-300:] or f"ssh exit {r.returncode}", "hint": hint}
    os_, arch, host, user, sudo = (r.stdout.strip().split("|") + [""] * 5)[:5]
    return {"ok": True, "os": os_, "arch": arch, "hostname": host, "user": user, "sudo": sudo}


def start_install(alias: str, coordinator: str | None = None) -> dict:
    with _lock:
        cur = _installs.get(alias)
        if cur and cur["state"] == "running":
            return cur
        os.makedirs(LOGS, exist_ok=True)
        log = os.path.join(LOGS, f"{alias}.log")
        st = {
            "state": "running",
            "started": time.time(),
            "finished": None,
            "rc": None,
            "log": log,
            "coordinator": coordinator or coordinator_ip(),
        }
        _installs[alias] = st
    kinds = read_roles().get(alias)

    def run():
        with open(log, "w") as f:
            f.write(f"# install {alias} -> coordinator {st['coordinator']} kinds={','.join(kinds) if kinds else 'all'}\n")
            f.flush()
            args = [INSTALLER, alias, st["coordinator"]] + ([",".join(kinds)] if kinds else [])
            try:
                p = subprocess.run(args, stdout=f, stderr=subprocess.STDOUT, timeout=2700)  # Chromium download can be slow
                rc = p.returncode
            except subprocess.TimeoutExpired:
                f.write("\n# timed out after 45 minutes\n")
                rc = 124
        try:
            with open(log) as g:
                needs_sudo = "NO SUDO" in g.read()
        except OSError:
            needs_sudo = False
        with _lock:
            st.update(
                state="needs-sudo" if rc == 0 and needs_sudo else "ok" if rc == 0 else "failed", finished=time.time(), rc=rc
            )

    threading.Thread(target=run, daemon=True).start()
    return st


def install_status(alias: str, tail: int = 400) -> dict:
    st = _installs.get(alias)
    log_path = os.path.join(LOGS, f"{alias}.log")
    text = ""
    try:
        with open(log_path, errors="replace") as f:
            lines = f.readlines()
        text = "".join(lines[-tail:])
    except FileNotFoundError:
        pass
    out = {k: v for k, v in (st or {}).items() if k != "log"}
    out["log"] = text
    out["state"] = out.get("state") or ("done-earlier" if text else "never")
    return out
