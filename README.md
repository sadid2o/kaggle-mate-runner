# kaggle-mate-runner

Cloud runner for **Kaggle Mate**. Starts and stops Kaggle notebooks on a
schedule so no PC ever has to be on.

This repo is **public**, deliberately — that is what makes GitHub-hosted
runners free and unlimited. Consequences, accepted and enforced:

- No Kaggle token, no Supabase key, no account username lives here.
- Per-account Kaggle tokens live in **Supabase Vault**.
- The two fixed keys live in **GitHub Secrets**.
- The app never talks to Kaggle directly, and never holds a Kaggle token.

## Layout

```
runner/
  schedule_engine.py   weekly schedule -> job rows, pure and I/O-free
  watchdog.py          injects the deadline cell into a push copy
  push_builder.py      assembles the folder handed to `kaggle kernels push`
  kaggle_cli.py        documented CLI calls, one isolated credential each
  supabase_client.py   PostgREST / Storage / Vault over plain HTTP
  tick.py              decides which jobs are due; creates them
  worker.py            start | cancel | reap a single job
  phase0.py            one-shot feasibility report
.github/workflows/
  tick.yml             the */5 heartbeat, fans out one runner per job
  worker.yml           manual tasks + hourly reap sweep
  phase0.yml           run the feasibility checks once
  tests.yml            pytest on every push
supabase/migrations/
  0001_init.sql        schema, RLS, claim RPC, Vault read RPC
  0002_cancel_channel.sql   the one RPC a publishable key may call
  0003_functions_support.sql  Vault write RPCs, account teardown, buckets
supabase/functions/
  accounts/ notebooks/ schedules/ jobs/ alerts/ status/
  _shared/             db, env, http, request, kaggle helpers
tests/
```

## How a run happens

1. `tick.yml` fires every 5 minutes and runs `runner.tick`.
2. Tick reads active schedules, asks `schedule_engine` what is due, and inserts
   job rows. A unique constraint on `(schedule_id, planned_start_at)` means two
   overlapping ticks cannot create the same job twice.
3. Tick publishes the new job ids to the workflow as a matrix.
4. One runner leg per job runs `runner.worker start:<job_id>`. The worker claims
   the row atomically (`km_claim_job`), so two legs can never run one job.
5. The worker downloads the stored notebook version, builds a push copy with the
   deadline watchdog injected, and pushes it with Kaggle's own timeout set
   slightly higher as a backstop.
6. It polls status, then stores the output log.

**Stopping is two-layer, and only one layer is reliable:**

| Layer | Stops at | Reliability |
|---|---|---|
| Injected watchdog cell | the exact deadline | the intended mechanism |
| Kaggle `session_timeout_seconds` | deadline + 300s | backstop only |

Kaggle documents no API to cancel a running batch kernel. The watchdog is
therefore a **workaround**, injected into a generated copy — the stored version
is never modified. R Markdown (`.Rmd`) cannot host a Python watchdog, so `.Rmd`
runs fall back to Kaggle's timeout alone; the worker logs a warning when that
happens.

## Secrets

Set in **Settings → Secrets and variables → Actions**:

| Secret | What | Where to get it |
|---|---|---|
| `SUPABASE_URL` | project URL | Supabase → Project Settings → API |
| `SUPABASE_SECRET_KEY` | server-side secret key | same page; never leaves the server |
| `SUPABASE_PUBLISHABLE_KEY` | the app's public key | same page; not a secret, but Actions needs it to relay cancels |
| `KAGGLE_TOKEN` | only for `phase0.yml` | Kaggle → Settings → API → Create New Token |

All four are required. The first three are read by `tick.yml` and `worker.yml`;
`phase0.yml` uses `KAGGLE_TOKEN` alone. Nothing needs setting by hand inside the
Edge Functions — Supabase injects `SUPABASE_URL`, `SUPABASE_SECRET_KEYS` and
`SUPABASE_PUBLISHABLE_KEYS` into them automatically, and `_shared/env.ts` reads
both the plural (injected) and singular (manual) names, so either works.

Per-account Kaggle tokens are **not** here. They are written to Supabase Vault
by an Edge Function and read one at a time by `km_get_account_secret`.

## Running it

Feasibility check first — it creates a throwaway private kernel on your account:

```
Actions → phase0 → Run workflow → confirm: run
```

Then read `.phase0/phase0-results.json` in the run's artifacts. It answers three
questions the whole design rests on:

- **Q1** does `kaggle kernels push` actually start a run?
- **Q2** does `--timeout` stop it at the deadline, and what status is left?
- **Q3** does the injected watchdog stop it — and does Kaggle report that as an
  *error*? If yes, a deadline stop is classified as `stopped`, not `failed`.

Do not build the app before Q2 and Q3 have real answers.

## Local development

Requires Python 3.12+.

```
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -v
```

The engine and watchdog tests are pure — no network, no credentials — so they
run anywhere. `phase0`, `tick` and `worker` need real credentials and are meant
to be run in Actions.

## Limits this design lives with

- Scheduled ticks can be **late or dropped** under load. Job times are computed
  deterministically, so a late tick still starts the job; a job whose window
  closes before anything runs is marked `missed`, not silently skipped.
- Minimum tick interval is **5 minutes**. Second-level precision is not claimed.
- A public repo's scheduled workflows **auto-disable after 60 days** without
  activity. `tick.yml` writes a keep-alive stamp each run so this does not bite;
  the app should still surface a "runner alive?" check.
- A single job may run at most **6 hours** on a GitHub-hosted runner, so the
  worker stops watching at 5.5h and hands over to `reap`.