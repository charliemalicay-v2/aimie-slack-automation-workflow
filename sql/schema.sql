-- Supabase / Postgres schema. Run once in the Supabase SQL editor (or via psql).
-- Mirrors app/models.py.

create table if not exists slack_events (
  id           bigserial primary key,
  event_id     varchar(64)  not null unique,           -- dedupe guard #1: Slack retries
  channel_id   varchar(32)  not null,
  message_ts   varchar(32)  not null,
  user_id      varchar(32),
  text         text         not null,
  raw          jsonb        not null,
  status       varchar(32)  not null default 'received',
  received_at  timestamptz  not null default now(),
  claimed_at   timestamptz,                             -- when processing started
  constraint uq_slack_events_channel_ts unique (channel_id, message_ts)  -- dedupe guard #2
);
create index if not exists ix_slack_events_status on slack_events (status);

create table if not exists classifications (
  id               bigserial primary key,
  event_pk         bigint      not null unique references slack_events(id),
  label            varchar(16) not null check (label in ('urgent','action','noise')),
  confidence       double precision not null check (confidence between 0 and 1),
  reason           text        not null,
  suggested_action text        not null,
  model            varchar(64) not null,
  is_fallback      boolean     not null default false,
  attempts         integer     not null default 1,
  created_at       timestamptz not null default now()
);

create table if not exists approval_requests (
  id               bigserial primary key,
  event_pk         bigint      not null unique references slack_events(id),
  label            varchar(16) not null,
  proposed_action  text        not null,
  status           varchar(16) not null default 'pending' check (status in ('pending','approved','rejected')),
  slack_message_ts varchar(32),
  decided_by       varchar(32),
  decided_at       timestamptz,
  created_at       timestamptz not null default now()
);
create index if not exists ix_approval_requests_status on approval_requests (status);

create table if not exists audit_log (
  id          bigserial primary key,
  at          timestamptz not null default now(),
  actor       varchar(64) not null,
  action      varchar(64) not null,
  entity_type varchar(32) not null,
  entity_id   varchar(64),
  details     jsonb       not null default '{}'::jsonb
);
create index if not exists ix_audit_log_entity on audit_log (entity_type, entity_id);

-- Append-only audit log: block UPDATE and DELETE at the database level.
create or replace function audit_log_immutable() returns trigger language plpgsql as $$
begin
  raise exception 'audit_log is append-only';
end $$;
drop trigger if exists trg_audit_log_immutable on audit_log;
create trigger trg_audit_log_immutable before update or delete on audit_log
  for each row execute function audit_log_immutable();

create table if not exists oauth_tokens (
  provider      varchar(32) primary key,
  access_token  text        not null,
  refresh_token text,
  expires_at    timestamptz,
  updated_at    timestamptz not null default now()
);

-- Supabase exposes public tables through its REST API. Enabling RLS with no policies
-- blocks anon/authenticated access; the service connects as the postgres role,
-- which bypasses RLS.
alter table slack_events      enable row level security;
alter table classifications   enable row level security;
alter table approval_requests enable row level security;
alter table audit_log         enable row level security;
alter table oauth_tokens      enable row level security;
