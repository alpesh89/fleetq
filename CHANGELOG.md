# Changelog

All notable changes to fleetq are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-26

First public release.

- Postgres-backed queue: `SELECT ... FOR UPDATE SKIP LOCKED` claims, leases with renewal, retries with
  backoff, idempotency keys, job budgets, depth limit, fan-out in the completing transaction.
- Worker: capability tags, per-kind adaptive concurrency, host resource guard, fleet-wide token
  buckets, per-site pacing at enqueue time, `LISTEN/NOTIFY` wake-up, control events, hot reload.
- Handler plug-ins: in-tree modules, directories (`--handlers`, `FLEETQ_HANDLERS`) and the
  `fleetq.handlers` entry-point group; per-handler machine setup (`fleetq setup`, `fleetq doctor`);
  capability tags earned from handlers (`TAG`).
- Built-in handlers: `sitemap`, `fetch` (change detection, bot-wall handling, redirect policy),
  `parse` (Trafilatura to Markdown, site signals), `render` (headless Chromium), `ocr_split` and
  `ocr_page` (offline OCR with Apple Vision or Tesseract).
- Dashboard (`fleetq dashboard`): agentless host monitor over SSH, worker install/update over SSH,
  queue console, documents, exports, PDF upload, JSON API with optional API keys.
- Deploy scripts for launchd (macOS, including the Local Network privacy workaround) and systemd.
