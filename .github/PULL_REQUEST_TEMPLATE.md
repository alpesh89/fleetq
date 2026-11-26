## What

<!-- One paragraph: what changes and why. Link the issue if there is one. -->

## Kind of change

- [ ] New handler (`fleetq/handlers/<kind>.py`) — payload and result documented in the module docstring
- [ ] Core (queue / worker / dashboard / deploy)
- [ ] Docs only

## Checklist

- [ ] `ruff check .` passes
- [ ] `python scripts/smoke.py` passes against a local Postgres
- [ ] Schema changes use `create table if not exists` / `add column if not exists`
- [ ] Network use goes through `ctx.pace_site` / `ctx.rate_limit`; a 429 raises `Defer(..., throttled=True)`
- [ ] `CHANGELOG.md` has a line under *Unreleased*
- [ ] First contribution? Added yourself to `CONTRIBUTORS.md`
