# Deploying a fleet

A fleet is one coordinator (Postgres, the dashboard, this repository checked out) and any number of
workers (Macs and Linux boxes on the same network). Workers only need SSH access from the
coordinator for installation; at run time they only talk to Postgres.

## 1. Coordinator

```
git clone https://github.com/alpesh89/fleetq && cd fleetq
docker compose up -d                         # Postgres on :5433, data in a named volume
uv venv .venv && uv pip install --python .venv/bin/python -e '.[ocr]'
cp .env.example .env                         # set FLEETQ_API_KEYS at least
.venv/bin/fleetq migrate
.venv/bin/fleetq dashboard                   # http://<coordinator>:8787
```

Give the coordinator a fixed address or a DHCP reservation: every worker's `.env` names it. If it
changes anyway, `deploy/install_fleet.sh <new ip>` rewrites every worker's `.env` and restarts them
without sudo.

To run the dashboard as a service on macOS, adapt `deploy/fleetq.agent.plist` (point it at
`.venv/bin/fleetq dashboard`); on Linux write a systemd unit like `deploy/fleetq.service` with
`ExecStart=/path/.venv/bin/fleetq dashboard`.

## 2. SSH access to workers

On the coordinator, one key for the fleet:

```
ssh-keygen -t ed25519 -f ~/.ssh/fleetq -N ""
ssh-copy-id -i ~/.ssh/fleetq.pub user@192.168.1.60        # once per worker
```

and a `Host` entry per worker in `~/.ssh/config` (the dashboard's **Add machine** writes it):

```
Host mini1
    HostName 192.168.1.60
    User admin
    IdentityFile ~/.ssh/fleetq
```

Add the alias to `inventory.txt` (one per line) and optionally restrict its handler kinds in
`deploy/roles.txt` (`mini1  fetch,parse,sitemap`). Unlisted hosts get every handler.

Workers need Python 3.11+ reachable as `python3` or `uv` will install one; `rsync` and `ssh`
on both ends. macOS workers need Remote Login enabled (System Settings → General → Sharing).

## 3. Install workers

From the dashboard: open a machine card, **Install / Update worker**, watch the log. Or from a shell:

```
deploy/install_worker.sh mini1 192.168.1.50                  # all handlers; 192.168.1.50 = coordinator
deploy/install_worker.sh box2  192.168.1.50 fetch,parse      # chosen handlers
deploy/install_fleet.sh 192.168.1.50                         # everything in inventory.txt, in parallel
```

The script rsyncs the checkout to `~/fleetq` on the host, creates a virtualenv with `uv`,
installs the package, runs `fleetq setup` for the chosen kinds (browsers, OCR engines, ...) and
installs a supervisor that restarts the worker on failure and on boot:

- **macOS**: a system LaunchDaemon (`/Library/LaunchDaemons/fleetq.worker.plist`) when sudo works
  without a password, otherwise it prints the one `sudo` command to run on that Mac. Logs:
  `~/Library/Logs/fleetq.log`.
- **Linux**: a systemd unit `fleetq` (`journalctl -u fleetq -f`). Without passwordless sudo it
  prints the command to run once.

Re-running the installer is the update path: it syncs code, re-runs setup, and asks the running
worker to restart. Workers show up in `fleetq status` and on the dashboard within 10 seconds.

### macOS and Local Network privacy

macOS blocks non-Apple binaries started by launchd from reaching other machines on the LAN until
someone clicks **Allow** on that Mac's screen, even for a system daemon. Processes started under
Remote Login (sshd) are allowed. So on macOS the service runs `deploy/run_worker.sh`, which hops
through `ssh localhost` with a key the installer creates for that purpose (`~/.ssh/fleetq_self`,
restricted in `authorized_keys` to loopback, no forwarding). Nothing else changes.

### Linux notes

- The `render` handler installs Chromium's system libraries with `playwright install --with-deps`,
  which needs sudo once; keep `render` to hosts where that is acceptable.
- Tesseract for `ocr_page`: `sudo apt-get install -y tesseract-ocr` (the setup step does this when
  sudo is passwordless).

## 4. Day to day

```
fleetq status                      # workers, slots, pressure, queue depth, jobs
fleetq ctl pause | resume          # all workers, or --target <id>
fleetq ctl reload                  # re-import handlers after syncing code
fleetq ctl dump                    # every worker logs where its in-flight tasks are
fleetq retry --status dead         # re-queue dead tasks
```

Workers that cannot reach the database cannot hear `ctl stop`; restart them over SSH with
`pkill -f "bin/fleetq worker"` and the supervisor relaunches them.

## 5. Security

The fleet trusts its network. Keep Postgres and the dashboard off the internet, set
`FLEETQ_API_KEYS`, and change the database password in `docker-compose.yml` / `.env` for anything
beyond a home lab. See `SECURITY.md`.
