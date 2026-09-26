-- 0002 — the cancel channel.
--
-- WHY THIS EXISTS
--
-- Kaggle has no supported way to stop a run started by `kaggle kernels push`.
-- That is verified against the shipped SDK, not assumed: `cancel_kernel_session`
-- exists and its route is `/api/v1/kernels/cancel-session/{kernel_session_id}`,
-- but nothing public ever returns a `kernel_session_id`. `push` answers with
-- ref/url/version_number/kernel_id; `get_kernel_session_status` answers with
-- status + failure_message; `create_kernel_session` answers with a generic
-- long-running `Operation`; and every call the CLI makes resolves owner/slug
-- server-side. The session id never leaves Kaggle.
--
-- So the only code that can stop a run is code *inside* the run. The injected
-- watchdog already handles the deadline (that covers both scheduled stop rules
-- — "after N hours" and "at a clock time" — and needs no network at all). This
-- migration adds the one thing the deadline cannot express: "Cancel now",
-- pressed at an arbitrary moment.
--
-- SECURITY SHAPE — read this before changing anything below
--
-- The notebook is code running on Kaggle's servers, and the user may publish
-- that notebook afterwards. So whatever it carries must be safe to publish.
-- Therefore:
--
--   * It carries the PUBLISHABLE key, never the secret key. The worker refuses
--     to build a folder otherwise (see `watchdog.inject`).
--   * This function is granted to `anon` — the role the publishable key maps to
--     — and to nothing else. Every other table in this schema has RLS on with
--     no anon policies, so `anon` can reach exactly one thing: this function.
--   * It returns a single boolean for a single job id. No job data, no account
--     data, no secret, and no way to list anything.
--   * The job UUID is the real capability. It is a v4 UUID, so it cannot be
--     guessed, and a leaked one reveals only whether one finished job was
--     cancelled. That is the whole blast radius, and it is stated plainly
--     rather than hidden behind the word "secure".

-- ------------------------------------------------------------------ the poll

create or replace function km_cancel_poll(p_job_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select coalesce(
        (select cancel_requested from jobs where id = p_job_id),
        false
    );
$$;

comment on function km_cancel_poll(uuid) is
    'Polled by the injected notebook watchdog. Returns only the cancel flag for '
    'one job id, so the publishable key can reach nothing else in this schema.';

-- Start from nothing, then open exactly the one door that is needed.
revoke all on function km_cancel_poll(uuid) from public, anon, authenticated;
grant execute on function km_cancel_poll(uuid) to anon;

-- ------------------------------------------------------------------ the stop
--
-- A job stopped via the cancel channel is still a normal end, and the worker
-- needs to tell "the user cancelled" apart from "the deadline arrived" when it
-- classifies the final Kaggle status. `stop_reason_pref` grew a third value for
-- that, and the CHECK below is what keeps a typo in the worker from silently
-- producing a job nobody can explain later.

alter table jobs drop constraint if exists jobs_stop_reason_pref_check;
alter table jobs
    add constraint jobs_stop_reason_pref_check
    check (stop_reason_pref in ('duration', 'clock_time', 'manual'));