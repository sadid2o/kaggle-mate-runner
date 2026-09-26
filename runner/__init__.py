"""Kaggle Mate cloud runner.

Host-agnostic by design: nothing here imports a GitHub Actions or Azure
specific API, so the same code runs under `schedule:` in Actions and under an
Azure Functions timer trigger. The host supplies two things only — a periodic
call to `tick.main()` and a per-job call to `worker.main()`.

Modules
-------
schedule_engine
    Weekly schedules -> concrete jobs with start, stop and timeout. Pure: no
    clock, no I/O, no database. Everything time-related is passed in, which is
    what makes the DST and midnight-crossing cases testable.
watchdog
    Generates the deadline cell prepended to a push copy. The one piece of the
    stop strategy that lives inside the user's run.
push_builder
    Assembles the folder handed to `kaggle kernels push`.
kaggle_cli
    The documented CLI calls, each with its own isolated credential directory
    so several accounts can be used in one process without leaking.
supabase_client
    PostgREST, Storage and the Vault RPC over plain HTTP.
tick
    Decides which jobs are due and creates them. Touches no Kaggle API.
worker
    Claims one job and does the heavy work: start, cancel or reap.
phase0
    One-shot feasibility report. Run once, read, then stop using it.
"""

__version__ = "0.1.0"