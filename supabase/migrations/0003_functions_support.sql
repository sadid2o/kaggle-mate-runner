-- 0003_functions_support.sql
--
-- The objects the Edge Functions need and nothing else can supply.
--
-- Three problems are solved here.
--
--   1. Vault cannot be written over PostgREST. `vault.create_secret` is a
--      relation in the `vault` schema, and the Data API exposes only `public`,
--      so the app-side function cannot call it no matter which key it holds.
--      The write side of the token lifecycle therefore needs the same
--      treatment the read side already got in 0001: a `security definer`
--      wrapper in `public`, whose body runs with the owner's rights.
--      `km_get_account_secret` is the read half; the three `km_vault_*`
--      functions below are the write half.
--
--   2. Deleting an account must take its Vault secret with it. The FK cascade
--      removes the account's notebooks, versions, schedules and jobs, but a
--      Vault secret is not a row in any of those tables, so it would survive
--      as an unreferenced encrypted blob. Both deletes happen in one function
--      so that no ordering can leave an account row alive with a
--      `vault_secret_id` pointing at nothing -- which the runner would read as
--      a working account whose token had silently vanished.
--
--   3. The `notebooks` and `jobs` buckets are used by the runner
--      (`storage_download("notebooks", version["code_path"])` and
--      `storage_upload("jobs", ...)`), but neither 0001 nor 0002 creates them,
--      and `supabase/functions/notebooks` cannot pull a version into a bucket
--      that does not exist. A bucket is a row in `storage.buckets`, so it is
--      created here rather than by hand in a dashboard, which keeps a fresh
--      project reproducible from the migrations alone.
--
-- Grants follow 0002's reasoning exactly: revoke from `public`, `anon` and
-- `authenticated`, then grant to `service_role` alone. These exist for the Edge
-- Functions, which hold the secret key; nothing reachable with the publishable
-- key may call them. The boundary matters most for the `vault` ones -- whoever
-- can read `vault.decrypted_secrets` holds every Kaggle token at once.

-- ---------------------------------------------------- vault, write side

-- Wrap `vault.create_secret` so the function can store a token it has been
-- handed without ever being able to read one back. The secret id is the only
-- thing returned; the caller keeps that and discards the token.
create or replace function km_vault_create_secret(
    p_secret      text,
    p_name        text default null,
    p_description text default null
)
returns uuid
language plpgsql
security definer
set search_path = public, vault
as $$
declare
    secret_id uuid;
begin
    -- An empty string would be stored happily and then fail later as
    -- "unauthorized" on the first run, which is the least diagnosable version
    -- of this mistake. Refuse it where the caller can still be told why.
    if p_secret is null or length(btrim(p_secret)) = 0 then
        raise exception 'secret must not be empty' using errcode = '22023';
    end if;

    secret_id := vault.create_secret(p_secret, p_name, p_description);
    return secret_id;
end;
$$;

comment on function km_vault_create_secret(text, text, text) is
    'Stores a secret and returns its Vault id. The only write path to Vault; callable by service_role only.';

-- Replacing a token, for the case where a user's Kaggle key was rotated.
-- Returns false rather than raising when the id is unknown, so the caller can
-- answer with a sentence naming the real problem instead of a 500.
create or replace function km_vault_update_secret(
    p_id     uuid,
    p_secret text
)
returns boolean
language plpgsql
security definer
set search_path = public, vault
as $$
begin
    if p_id is null or p_secret is null or length(btrim(p_secret)) = 0 then
        return false;
    end if;

    -- `vault.update_secret` with an id that does not exist is not documented to
    -- be a no-op, so existence is checked here rather than assumed.
    if not exists (select 1 from vault.secrets where id = p_id) then
        return false;
    end if;

    perform vault.update_secret(p_id, p_secret);
    return true;
end;
$$;

comment on function km_vault_update_secret(uuid, text) is
    'Replaces an existing secret in place. Returns false when the id is unknown.';

-- Vault documents no delete function, only create and update. Its secrets live
-- in the documented `vault.secrets` relation, so the removal is a plain DELETE
-- against that table rather than a call to an invented function.
create or replace function km_vault_delete_secret(p_id uuid)
returns boolean
language plpgsql
security definer
set search_path = public, vault
as $$
begin
    if p_id is null then
        return false;
    end if;

    delete from vault.secrets where id = p_id;
    return found;
end;
$$;

comment on function km_vault_delete_secret(uuid) is
    'Removes a secret from Vault. Returns false when the id was already gone.';

-- ---------------------------------------------------- account teardown

-- One function, not two calls from the Edge Function, because the failure mode
-- of two calls is asymmetric: delete-the-row-first leaves an orphan secret
-- (harmless but permanent), delete-the-secret-first leaves an account whose
-- token has vanished (a live account that fails on every run). Neither is
-- acceptable when a single transaction avoids both.
create or replace function km_delete_account(p_account_id uuid)
returns boolean
language plpgsql
security definer
set search_path = public, vault
as $$
declare
    secret_id uuid;
begin
    select a.vault_secret_id into secret_id
      from public.accounts a
     where a.id = p_account_id
       for update;

    if not found then
        return false;
    end if;

    if secret_id is not null then
        delete from vault.secrets where id = secret_id;
    end if;

    -- Everything else -- notebooks, notebook_versions, schedules, jobs, events,
    -- artifacts, alerts -- goes with it through the existing FK cascades.
    delete from public.accounts where id = p_account_id;
    return true;
end;
$$;

comment on function km_delete_account(uuid) is
    'Deletes an account and its Vault secret together, so neither can outlive the other.';

-- ---------------------------------------------------- grants

-- Explicitly to service_role, not implicitly through PUBLIC: 0002 already
-- established that a publishable key must reach nothing but `km_cancel_poll`.
revoke all on function km_vault_create_secret(text, text, text) from public, anon, authenticated;
revoke all on function km_vault_update_secret(uuid, text)       from public, anon, authenticated;
revoke all on function km_vault_delete_secret(uuid)             from public, anon, authenticated;
revoke all on function km_delete_account(uuid)                  from public, anon, authenticated;

grant execute on function km_vault_create_secret(text, text, text) to service_role;
grant execute on function km_vault_update_secret(uuid, text)       to service_role;
grant execute on function km_vault_delete_secret(uuid)             to service_role;
grant execute on function km_delete_account(uuid)                  to service_role;

-- ---------------------------------------------------- storage buckets

-- Private, like the layout in docs/DB_SCHEMA.md says: the app never reads these
-- directly, the runner reads and writes them with the secret key.
insert into storage.buckets (id, name, public)
values ('notebooks', 'notebooks', false)
on conflict (id) do nothing;

insert into storage.buckets (id, name, public)
values ('jobs', 'jobs', false)
on conflict (id) do nothing;

-- No storage policies are created. Both buckets are reached only by the runner
-- and the `notebooks` Edge Function, each holding the secret key, which bypasses
-- RLS. A policy here would be dead weight that could only ever widen access.