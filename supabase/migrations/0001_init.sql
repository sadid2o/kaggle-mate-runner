-- Kaggle Mate — core schema.
--
-- Three properties this file is responsible for, none of which belong in
-- application code because none of them can be enforced reliably there:
--
--   1. A schedule can never produce two jobs for the same start instant.
--      `unique (schedule_id, planned_start_at)` — the database decides, so two
--      overlapping ticks cannot both start a run.
--   2. A job can never be claimed by two workers.
--      `km_claim_job` locks the row and moves it in one statement.
--   3. A Kaggle token can only be read one account at a time, by the runner.
--      `km_get_account_secret` is security definer; the table stays unexposed.
--
-- Everything is RLS-enabled with no anon policies. The app talks to Postgres
-- only through Edge Functions holding the service key.

create extension if not exists "pgcrypto";
create extension if not exists "pg_cron";
create extension if not exists "pg_net";

-- ---------------------------------------------------------------- accounts

create table if not exists accounts (
    id             uuid primary key default gen_random_uuid(),
    label          text not null,                     -- what the user calls it
    kaggle_username text not null,
    -- A Vault secret id, never the token itself.
    vault_secret_id uuid,
    is_active      boolean not null default true,
    last_verified_at timestamptz,
    last_error     text,
    created_at     timestamptz not null default now(),
    -- One row per Kaggle account. Two rows sharing a username would mean two
    -- different tokens for the same identity, which can only cause confusion.
    unique (kaggle_username)
);

-- --------------------------------------------------------------- notebooks

create table if not exists notebooks (
    id           uuid primary key default gen_random_uuid(),
    account_id   uuid not null references accounts(id) on delete cascade,
    -- "owner/slug" exactly as Kaggle writes it.
    kaggle_ref   text not null,
    display_name text not null,
    is_active    boolean not null default true,
    last_seen_at timestamptz,
    created_at   timestamptz not null default now(),
    -- The same notebook can live on two accounts (forks); it may not appear
    -- twice on the same one.
    unique (account_id, kaggle_ref)
);

-- Every pulled version is kept forever. This is what makes "the original is
-- never modified" checkable: the bytes we pushed are the bytes in row v_n,
-- and they are never overwritten.
create table if not exists notebook_versions (
    id          uuid primary key default gen_random_uuid(),
    notebook_id uuid not null references notebooks(id) on delete cascade,
    version     integer not null,
    code_path   text not null,        -- Storage: notebooks/{id}/v{n}/source
    meta_path   text not null,        -- Storage: notebooks/{id}/v{n}/metadata
    code_file   text not null,        -- metadata's own code_file value
    language    text not null default 'python',
    pulled_at   timestamptz not null default now(),
    unique (notebook_id, version)
);

-- --------------------------------------------------------------- schedules

create table if not exists schedules (
    id               uuid primary key default gen_random_uuid(),
    notebook_id      uuid not null references notebooks(id) on delete cascade,
    -- 0 = Monday .. 6 = Sunday, matching the runner's engine.
    days_of_week     smallint[] not null,
    start_time       time not null,
    duration_minutes integer not null check (duration_minutes > 0),
    -- Optional wall-clock stop; whichever of this and start+duration comes
    -- first is the stop the runner aims for.
    stop_at_time     time,
    timezone         text not null default 'Asia/Dhaka',
    start_date       date,
    end_date         date,
    overrides        jsonb not null default '{}'::jsonb,
    is_active        boolean not null default true,
    created_at       timestamptz not null default now(),
    check (days_of_week <@ array[0,1,2,3,4,5,6]::smallint[]),
    check (cardinality(days_of_week) > 0),
    check (end_date is null or start_date is null or end_date >= start_date)
);

create index if not exists schedules_active_idx on schedules (is_active) where is_active;

-- -------------------------------------------------------------------- jobs

create type job_state as enum (
    'pending',    -- created by tick, waiting for a worker
    'claimed',    -- a worker has taken it and is starting it
    'running',    -- Kaggle accepted the push
    'watching',   -- worker's watch window ended; reap will finish it
    'stopped',    -- stopped at its deadline (the normal scheduled outcome)
    'completed',  -- notebook ended on its own
    'failed',
    'cancelled',
    'missed'      -- window closed before anything could start it
);

create table if not exists jobs (
    id               uuid primary key default gen_random_uuid(),
    schedule_id      uuid references schedules(id) on delete set null,
    notebook_id      uuid not null references notebooks(id) on delete cascade,
    account_id       uuid not null references accounts(id) on delete cascade,
    kaggle_version_id uuid references notebook_versions(id) on delete set null,

    state            job_state not null default 'pending',
    planned_start_at timestamptz not null,
    planned_stop_at  timestamptz not null,
    started_at       timestamptz,
    finished_at      timestamptz,

    -- Kaggle's own limit. Always larger than the watchdog deadline, so Kaggle
    -- is the backstop and not the thing we are aiming at.
    timeout_seconds  integer not null check (timeout_seconds > 0),
    stop_reason_pref text not null default 'duration',
    stop_reason      text,
    error_code       text,
    last_status      text,
    log_path         text,
    -- Set by the cancel path; the in-notebook watchdog polls it.
    cancel_requested boolean not null default false,

    created_at       timestamptz not null default now(),

    check (planned_stop_at > planned_start_at)
);

-- THE double-dispatch guard. A manual "Run now" has schedule_id null and so
-- escapes it — which is intended: a manual run is not the same event as a
-- scheduled one, and the user asking for it twice means it twice.
create unique index if not exists jobs_one_per_schedule_instant
    on jobs (schedule_id, planned_start_at)
    where schedule_id is not null;

create index if not exists jobs_pending_idx on jobs (planned_start_at) where state = 'pending';
create index if not exists jobs_live_idx on jobs (state) where state in ('claimed','running','watching');

-- -------------------------------------------------------------- job_events

create table if not exists job_events (
    id         bigserial primary key,
    job_id     uuid not null references jobs(id) on delete cascade,
    level      text not null default 'info' check (level in ('debug','info','warn','error')),
    message    text not null,
    payload    jsonb,
    at         timestamptz not null default now()
);

create index if not exists job_events_job_idx on job_events (job_id, at desc);

-- --------------------------------------------------------------- artifacts

create table if not exists artifacts (
    id         uuid primary key default gen_random_uuid(),
    job_id     uuid not null references jobs(id) on delete cascade,
    kind       text not null,        -- 'log', 'output_zip', ...
    path       text not null,        -- Storage: jobs/{job_id}/...
    size_bytes bigint,
    created_at timestamptz not null default now()
);

-- ------------------------------------------------------------------ alerts

-- In-app only: the app reads this table when the user opens it. Deliberately
-- not push notifications — v1 has no requirement to wake a closed app.
create table if not exists alerts (
    id         bigserial primary key,
    job_id     uuid references jobs(id) on delete cascade,
    severity   text not null default 'info' check (severity in ('info','warn','error')),
    title      text not null,
    body       text,
    read_at    timestamptz,
    created_at timestamptz not null default now()
);

create index if not exists alerts_unread_idx on alerts (created_at desc) where read_at is null;

-- ---------------------------------------------------------------- settings

create table if not exists settings (
    key        text primary key,
    value      jsonb not null,
    updated_at timestamptz not null default now()
);

insert into settings (key, value) values
    ('horizon_minutes', '10'::jsonb),
    ('grace_minutes', '30'::jsonb),
    ('stop_buffer_seconds', '300'::jsonb),
    ('keep_versions', 'true'::jsonb)
on conflict (key) do nothing;

-- ------------------------------------------------------------------- RLS
-- Enabled everywhere, no policies for anon/authenticated. The only key that
-- can read these tables is the service role, which lives server-side in the
-- runner and in Edge Functions — never in the APK.

alter table accounts          enable row level security;
alter table notebooks         enable row level security;
alter table notebook_versions enable row level security;
alter table schedules         enable row level security;
alter table jobs              enable row level security;
alter table job_events        enable row level security;
alter table artifacts         enable row level security;
alter table alerts            enable row level security;
alter table settings          enable row level security;

-- ------------------------------------------------------------ claim a job

-- Takes at most one job, atomically. `for update skip locked` is the load-
-- bearing clause: two workers running at the same instant both call this, and
-- the second gets no row instead of the same row.
create or replace function km_claim_job(p_mode text, p_job_id uuid default null)
returns setof jobs
language plpgsql
security definer
set search_path = public
as $$
declare
    target jobs;
begin
    if p_mode = 'reap' then
        select * into target
        from jobs
        where state in ('claimed','running','watching')
          and planned_stop_at < now() - interval '2 minutes'
        order by planned_stop_at
        for update skip locked
        limit 1;
    elsif p_mode = 'start' then
        select * into target
        from jobs
        where (p_job_id is null or id = p_job_id)
          and state = 'pending'
        order by planned_start_at
        for update skip locked
        limit 1;
    elsif p_mode = 'cancel' then
        -- A cancel must work whether the job is waiting to start or already
        -- running: the user pressing "Cancel" does not know or care which.
        --
        -- `watching` belongs here and was missing. It means a worker has
        -- stepped back but the run is still going on Kaggle -- precisely a job
        -- a user may want to stop. Omitting it made the UPDATE below match
        -- nothing, so the call returned zero rows and the app showed a cancel
        -- that silently did nothing. The state is deliberately left as
        -- `watching` by the CASE below: it is not in the finalise set
        -- ('pending','claimed'), so this only raises `cancel_requested` and
        -- returns the row, which tells the caller to wait for the cooperative
        -- stop instead of claiming the job was already cancelled.
        select * into target
        from jobs
        where (p_job_id is null or id = p_job_id)
          and state in ('pending','claimed','running','watching')
        order by planned_start_at
        for update skip locked
        limit 1;
    else
        raise exception 'unknown mode %', p_mode;
    end if;

    if target.id is null then
        return;
    end if;

    update jobs
       set state = case p_mode
                       when 'start'  then 'claimed'::job_state
                       when 'cancel' then
                           -- Not started yet -> finalise now, nothing to stop.
                           case when target.state in ('pending','claimed')
                                then 'cancelled'::job_state
                                else target.state
                           end
                       else 'watching'::job_state
                   end,
           cancel_requested = case when p_mode = 'cancel' then true else cancel_requested end,
           stop_reason = case
                             when p_mode = 'cancel' and target.state in ('pending','claimed')
                             then 'cancelled_by_user'
                             else stop_reason
                         end,
           error_code  = case
                             when p_mode = 'cancel' and target.state in ('pending','claimed')
                             then 'CANCELLED_BY_USER'
                             else error_code
                         end,
           finished_at = case
                             when p_mode = 'cancel' and target.state in ('pending','claimed')
                             then now()
                             else finished_at
                         end
     where id = target.id
    returning * into target;

    -- The returned state IS the signal: 'cancelled' means the claim already
    -- finalised it (nothing was running), anything else means the caller must
    -- wait for the cooperative stop to land.
    return next target;
end;
$$;

-- ------------------------------------------------- read one account secret

-- The only path to a Kaggle token. Returns exactly one account's credential,
-- so a compromised runner cannot dump every token in one call. `security
-- definer` lets it read the Vault view, which is otherwise unreachable.
create or replace function km_get_account_secret(p_account_id uuid)
returns table (account_id uuid, kaggle_username text, secret text)
language sql
security definer
set search_path = public, vault
as $$
    select a.id, a.kaggle_username, s.decrypted_secret
      from accounts a
      join vault.decrypted_secrets s on s.id = a.vault_secret_id
     where a.id = p_account_id
       and a.is_active;
$$;

-- Vault secrets are read only through the function above.
revoke all on function km_get_account_secret(uuid) from public, anon, authenticated;
revoke all on function km_claim_job(text, uuid) from public, anon, authenticated;

-- ---------------------------------------------------------------- housekeeping

-- Old events are pruned but artifacts never are: the user asked to keep
-- history indefinitely, and a log is the record of what a run actually did.
create or replace function km_prune_events(p_keep_days int default 180)
returns integer
language sql
security definer
set search_path = public
as $$
    with gone as (
        delete from job_events
         where at < now() - make_interval(days => p_keep_days)
        returning 1
    )
    select count(*)::int from gone;
$$;