#!/usr/bin/env bash
# Install or update the worker on every host in inventory.txt, in parallel.
#   deploy/install_fleet.sh 192.168.1.50            # coordinator IP (where Postgres runs)
#   deploy/install_fleet.sh 192.168.1.50 mini1 box2   # only these hosts
# Handler kinds per host come from deploy/roles.txt; unlisted hosts get everything.
set -uo pipefail
COORD="${1:?coordinator ip required}"; shift || true
HERE="$(cd "$(dirname "$0")" && pwd)"
LOGS="$HERE/../.deploy-logs"; mkdir -p "$LOGS"

hosts=("$@")
if [ ${#hosts[@]} -eq 0 ]; then
  while read -r line; do
    line="${line%%#*}"; line="$(echo "$line" | xargs)"
    [ -n "$line" ] && hosts+=("$line")
  done < "$HERE/../inventory.txt"
fi

kinds_for() { awk -v h="$1" '$1==h {print $2}' "$HERE/roles.txt" 2>/dev/null | head -1; }

pids=()
for h in "${hosts[@]}"; do
  k="$(kinds_for "$h")"
  echo "-> $h ${k:+($k)}"
  "$HERE/install_worker.sh" "$h" "$COORD" "$k" > "$LOGS/$h.log" 2>&1 &
  pids+=($!)
done

fail=0
for i in "${!hosts[@]}"; do
  if wait "${pids[$i]}"; then echo "ok   ${hosts[$i]}"; else echo "FAIL ${hosts[$i]}  (see .deploy-logs/${hosts[$i]}.log)"; fail=1; fi
done
exit $fail
