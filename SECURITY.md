# Security policy

## Scope

fleetq is designed for a **private network you control**: the queue database, the workers and the
dashboard all trust each other. Specifically:

- The dashboard's HTTP API runs **open** unless `FLEETQ_API_KEYS` is set. Never expose it to the
  internet without keys, and preferably not at all; put it behind a VPN or an authenticating proxy.
- Workers connect to Postgres with one shared role. Anyone who can reach the database can submit
  work and read results.
- The worker installer and the host monitor use SSH keys from `~/.ssh/config` on the coordinator.
- Handlers run arbitrary Python in the worker process. Only install handlers you trust.

## Reporting a vulnerability

Please do not open a public issue for security problems. Email the maintainer (see the repository's
GitHub profile) or use GitHub's private vulnerability reporting on this repository. Include the
version, how to reproduce, and the impact you see. You will get an acknowledgement within a few days
and a fix or a mitigation as soon as one exists; credit is given in the changelog unless you prefer
otherwise.

## Supported versions

Only the latest release on the `main` branch receives fixes.
