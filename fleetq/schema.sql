-- fleetq schema. Idempotent: `fleetq migrate` and every worker start run it; add new tables and
-- columns with `if not exists` so existing databases upgrade in place.

create table if not exists workers (
  id          text primary key,
  host        text,
  pid         integer,
  tags        text[] not null default '{}',
  kinds       text[] not null default '{}',
  addr        text,
  slots       jsonb not null default '{}',
  started_at  timestamptz not null default now(),
  last_seen   timestamptz not null default now()
);

create table if not exists jobs (
  id          bigserial primary key,
  name        text,
  max_tasks   integer,
  task_count  integer not null default 0,
  created_at  timestamptz not null default now()
);

create table if not exists tasks (
  id           bigserial primary key,
  job_id       bigint references jobs(id) on delete cascade,
  parent_id    bigint,
  depth        integer not null default 0,
  kind         text not null,
  version      integer not null default 1,
  payload      jsonb not null default '{}',
  idem_key     text unique,
  needs        text[] not null default '{}',
  priority     integer not null default 0,
  depends_on   bigint[] not null default '{}',
  status       text not null default 'ready'
               check (status in ('ready','running','done','failed','dead','cancelled')),
  claimed_by   text,
  lease_until  timestamptz,
  attempts     integer not null default 0,
  max_attempts integer not null default 5,
  run_after    timestamptz not null default now(),
  result       jsonb,
  error        text,
  created_at   timestamptz not null default now(),
  started_at   timestamptz,
  finished_at  timestamptz
);
create index if not exists tasks_ready_idx   on tasks (kind, priority desc, id) where status = 'ready';
create index if not exists tasks_running_idx on tasks (lease_until) where status = 'running';
create index if not exists tasks_job_idx     on tasks (job_id);
create index if not exists tasks_parent_idx  on tasks (parent_id);
create index if not exists tasks_status_idx  on tasks (status, kind);
-- Claims and demand counts look only at tasks that are due; with per-site pacing most ready rows
-- are not, so the scan must start at run_after rather than wade through every paced row.
create index if not exists tasks_due_idx     on tasks (kind, run_after) where status = 'ready';

create table if not exists rate_limits (
  name           text primary key,
  capacity       numeric not null,
  tokens         numeric not null,
  refill_per_sec numeric not null,
  updated_at     timestamptz not null default now()
);
-- Pacing cursor: the moment up to which this site's queued requests are already spread out.
alter table rate_limits add column if not exists scheduled_until timestamptz;
-- Site health: consecutive failed requests (timeouts, 5xx, 429, bot walls) and when the streak began.
alter table rate_limits add column if not exists fail_streak integer not null default 0;
alter table rate_limits add column if not exists down_since timestamptz;

create table if not exists events (
  id          bigserial primary key,
  topic       text not null,
  body        jsonb not null default '{}',
  created_at  timestamptz not null default now()
);

-- Built-in handler tables -------------------------------------------------

create table if not exists blobs (
  key          text primary key,
  content_type text,
  data         bytea not null,
  task_id      bigint,
  created_at   timestamptz not null default now()
);
alter table blobs add column if not exists encoding text;          -- null = raw bytes, 'zstd' = zstandard frame
alter table blobs alter column data set storage external;          -- we compress ourselves; skip Postgres' pglz pass

create table if not exists documents (
  id             bigserial primary key,
  url            text unique not null,
  title          text,
  markdown       text,
  word_count     integer,
  parser_version integer,
  job_id         bigint,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
alter table documents add column if not exists links jsonb;   -- [{"href","text"}] all anchors, incl. nav/footer

-- Wake workers the moment a task is inserted or re-readied ------------------

create or replace function fleetq_notify_task() returns trigger language plpgsql as $$
begin
  if new.status = 'ready' then
    perform pg_notify('fleetq_tasks', new.kind);
  end if;
  return new;
end $$;

drop trigger if exists tasks_notify on tasks;
create trigger tasks_notify after insert or update of status on tasks
  for each row execute function fleetq_notify_task();

create or replace function fleetq_notify_event() returns trigger language plpgsql as $$
begin
  perform pg_notify('fleetq_control', new.topic || ' ' || new.body::text);
  return new;
end $$;

drop trigger if exists events_notify on events;
create trigger events_notify after insert on events
  for each row execute function fleetq_notify_event();

-- Per-URL memory for change detection (works for every site, sitemap or not) -----------
create table if not exists pages (
  url             text primary key,
  first_seen      timestamptz not null default now(),
  fetched_at      timestamptz not null default now(),   -- last time we confirmed the content (fetch, 304 or hash match)
  changed_at      timestamptz,                          -- last time the content actually changed
  change_count    integer not null default 0,
  status          integer,
  bytes           integer,
  content_hash    text,
  etag            text,
  last_modified   text,
  sitemap_lastmod text,
  last_job_id     bigint
);
create index if not exists pages_fetched_idx on pages (fetched_at);
alter table pages add column if not exists redirect_to text;   -- host this URL redirected to off-site, if it did

-- Marketing/platform signals per site, a union over every parsed page (see fleetq/signals.py).
create table if not exists site_signals (
  host         text primary key,                 -- without www.
  url          text,                             -- last page that contributed
  platform     text,
  builder      text,
  tags         jsonb not null default '{}',      -- {"meta_pixel": ["<id>"], "ga4": ["G-..."], ...}
  pages        integer not null default 0,
  detected_at  timestamptz not null default now()
);
