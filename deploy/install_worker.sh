#!/usr/bin/env bash
# Install or update the fleetq worker on one host over SSH (macOS or Ubuntu).
#   deploy/install_worker.sh mini1 192.168.1.10               # all handlers
#   deploy/install_worker.sh box2  192.168.1.10 fetch,parse,render
# Arguments: ssh alias from ~/.ssh/config, coordinator IP (Postgres host), optional handler kinds.
set -euo pipefail
HOST="$1"; COORD="$2"; KINDS="${3:-}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

ssh "$HOST" 'mkdir -p ~/fleetq'
rsync -az --delete --exclude .venv --exclude '__pycache__' --exclude .git --exclude .deploy-logs \
  "$SRC/fleetq" "$SRC/pyproject.toml" "$SRC/README.md" "$SRC/deploy" "$HOST:~/fleetq/"

ssh "$HOST" bash -s "$COORD" "$KINDS" <<'REMOTE'
set -euo pipefail
COORD="$1"; KINDS="${2:-}"
cd ~/fleetq
# Coordinator address for the worker, read by fleetq/config.py and preferred over any FLEETQ_DSN a
# service file sets. Rewritten on every install so a changed address needs no sudo and no unit edit.
# (This is the worker's own .env; the project's .env with API keys is never synced.)
printf 'FLEETQ_COORDINATOR=%s\n' "$COORD" > .env
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"   # non-interactive ssh shells miss uv's install dir
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
[ -x .venv/bin/python ] || uv venv -q .venv
uv pip install -q --python .venv/bin/python -e .
# Per-handler machine setup (browsers, parsers, ...). Idempotent.
if [ -n "$KINDS" ]; then .venv/bin/fleetq setup --kinds "$KINDS" || echo "WARNING: some handler setups failed (see above); the worker will run the others"
else .venv/bin/fleetq setup || echo "WARNING: some handler setups failed (see above); the worker will run the others"; fi
OS="$(uname -s)"

# Ask the running worker to exit so its supervisor relaunches it with the new code and .env.
# The polite path goes through the database; a worker that lost the coordinator (address change)
# never hears it, so after a grace period the process is killed outright.
restart_worker() {
  FLEETQ_COORDINATOR="$COORD" .venv/bin/fleetq ctl stop --target "$1" >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7 8; do pgrep -f "[f]leetq worker" >/dev/null || break; sleep 1; done
  if pgrep -f "[f]leetq worker" >/dev/null; then
    pkill -9 -f "[f]leetq worker" && echo "$2 killed (did not answer stop); supervisor relaunches it with new code"
  else
    echo "$2 asked to restart (picks up new code)"
  fi
}

if [ "$OS" = "Darwin" ]; then
  # Self-SSH key so the worker can run under sshd (see deploy/run_worker.sh). Generated on this
  # machine, authorizes only this user on this machine, never leaves it. No sudo needed.
  if [ ! -f ~/.ssh/fleetq_self ]; then
    mkdir -p ~/.ssh && chmod 700 ~/.ssh
    ssh-keygen -q -t ed25519 -N "" -C "fleetq self-hop $(hostname -s)" -f ~/.ssh/fleetq_self
  fi
  PUB="$(cut -d' ' -f1-2 ~/.ssh/fleetq_self.pub)"
  grep -qF "$PUB" ~/.ssh/authorized_keys 2>/dev/null || echo "from=\"127.0.0.1,::1\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding $PUB fleetq-self-hop" >> ~/.ssh/authorized_keys
  chmod 600 ~/.ssh/authorized_keys
  if ssh -i ~/.ssh/fleetq_self -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o LogLevel=ERROR localhost true; then
    echo "self-ssh hop ok"
  else
    echo "WARNING: self-ssh failed (Remote Login off?). Worker will try a direct connection, which macOS may block."
  fi
  mkdir -p deploy/rendered
  SUB=(-e "s|__USER__|$USER|g" -e "s|__HOSTNAME__|$(hostname -s)|g" -e "s|__COORDINATOR__|$COORD|g")
  sed "${SUB[@]}" deploy/fleetq.agent.plist        > deploy/rendered/agent.plist
  sed "${SUB[@]}" deploy/fleetq.daemon.plist > deploy/rendered/daemon.plist
  # A user-session agent is blocked from the LAN by macOS Local Network privacy until someone clicks
  # Allow on screen. A system daemon is exempt, so prefer it whenever sudo works without a password.
  launchctl bootout "gui/$(id -u)/fleetq.worker" 2>/dev/null || true
  rm -f ~/Library/LaunchAgents/fleetq.worker.plist
  if sudo -n true 2>/dev/null; then
    sudo launchctl bootout system/fleetq.worker 2>/dev/null || true
    sudo cp deploy/rendered/daemon.plist /Library/LaunchDaemons/fleetq.worker.plist
    sudo chown root:wheel /Library/LaunchDaemons/fleetq.worker.plist
    sudo launchctl bootstrap system /Library/LaunchDaemons/fleetq.worker.plist
    echo "launchd DAEMON installed; log: ~/Library/Logs/fleetq.log"
  elif [ -f /Library/LaunchDaemons/fleetq.worker.plist ]; then
    # Daemon installed earlier by hand. No sudo here, so ask the running worker to exit; KeepAlive relaunches it with the new code.
    restart_worker "$(hostname -s)" daemon
  else
    echo "NO SUDO: worker not started. Run this once from the coordinator (then re-runs here will just sync code):"
    echo "  ssh -t <alias> 'sudo cp ~/fleetq/deploy/rendered/daemon.plist /Library/LaunchDaemons/fleetq.worker.plist && sudo launchctl bootstrap system /Library/LaunchDaemons/fleetq.worker.plist'"
  fi
else
  mkdir -p deploy/rendered
  sed -e "s|__USER__|$USER|g" -e "s|__COORDINATOR__|$COORD|g" deploy/fleetq.service > deploy/rendered/fleetq.service
  if ! sudo -n true 2>/dev/null; then
    if systemctl is-active --quiet fleetq 2>/dev/null; then
      restart_worker "$(hostname)" service
    else
      echo "NO SUDO: worker not started. Run this once from the coordinator:"
      echo "  ssh -t <alias> 'sudo cp ~/fleetq/deploy/rendered/fleetq.service /etc/systemd/system/fleetq.service && sudo systemctl daemon-reload && sudo systemctl enable --now fleetq'"
    fi
    exit 0
  fi
  sudo cp deploy/rendered/fleetq.service /etc/systemd/system/fleetq.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now fleetq
  sudo systemctl restart fleetq
  echo "systemd unit installed; log: journalctl -u fleetq -f"
fi
REMOTE
