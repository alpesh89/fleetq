#!/bin/bash
# Started by launchd (macOS) or systemd (Linux). Keeps worker start logic out of the service
# files so it can change without sudo.
#
# macOS: Local Network privacy blocks non-Apple binaries launched by launchd from reaching the
# LAN ("No route to host"), while anything running under Remote Login (sshd) is allowed. So on
# macOS we hop through ssh to ourselves with a key the installer creates for exactly this.
cd "$(dirname "$0")/.." || exit 1
# The coordinator address lives in ~/fleetq/.env (FLEETQ_COORDINATOR, rewritten by the installer on
# every run); fleetq/config.py prefers it over any FLEETQ_DSN a service file passes through here.
ID="${FLEETQ_WORKER_ID:-$(hostname -s)}"
ENV_PASS=""
for v in FLEETQ_DSN FLEETQ_LEASE FLEETQ_MAX_LOAD FLEETQ_MAX_CPU FLEETQ_MIN_FREE_MEM FLEETQ_MIN_FREE_MEM_GB FLEETQ_MAX_INFLIGHT FLEETQ_NICE; do
  [ -n "${!v:-}" ] && ENV_PASS="$ENV_PASS $v='${!v}'"
done
if [ "$(uname -s)" = "Darwin" ] && [ -f "$HOME/.ssh/fleetq_self" ]; then
  exec /usr/bin/ssh -i "$HOME/.ssh/fleetq_self" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
       -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o LogLevel=ERROR localhost \
       "cd $PWD && export $ENV_PASS && exec .venv/bin/fleetq worker --id '$ID'"
fi
exec .venv/bin/fleetq worker --id "$ID"
