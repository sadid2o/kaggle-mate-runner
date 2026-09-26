"""Supabase access — PostgREST, Storage and the Vault RPC.

Uses the official `supabase-py` client rather than hand-rolled HTTP.

Why this matters, and why it was rewritten: Supabase's current **secret** keys
(`sb_secret_...`) are opaque strings, NOT JWTs. Sending one as
`Authorization: Bearer` makes the platform try to parse it as a JWT and reject
the request — the documented error is `Invalid JWT`. A hand-written client that
set both headers looked correct and would have failed on every single call. The
official client sends the key on `apikey` only, which is the current rule.

Sources: https://supabase.com/docs/guides/getting-started/api-keys and
https://supabase.com/docs/guides/getting-started/migrating-to-new-api-keys
(checked 2026-09-26).
"""

from __future__ import annotations

from supabase import Client, create_client  # noqa: F401  (Client for type hints)

#: Operations the PostgREST filter parser below understands. Kept small on
#: purpose — an unrecognised operator should be a loud error, not a silently
#: ignored filter that returns the wrong rows.
_OPERATORS = {"eq", "neq", "lt", "lte", "gt", "gte", "like", "is", "in"}


class SupabaseError(RuntimeError):
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


class SupabaseClient:
    """Server-side access to one Supabase project.

    The secret key bypasses RLS, which is the point: this client runs in the
    cloud runner and in Edge Functions, never in the app. Every table it touches
    has RLS enabled with no anon policies, so this key is the only thing that
    can read them.
    """

    def __init__(self, url: str, secret_key: str):
        self.url = url.rstrip("/")
        self.client = create_client(self.url, secret_key)

    # ---- PostgREST -----------------------------------------------------

    def select(self, table: str, **params) -> list[dict]:
        """Select rows. Values use the ``"op.value"`` form, e.g. ``is_active="eq.true"``."""
        columns = params.pop("select", "*")
        query = self.client.table(table).select(columns)

        for key, value in params.items():
            text = str(value)
            op, _, operand = text.partition(".")
            if key == "order":
                query = _apply_order(query, text)
            elif key == "limit":
                query = query.limit(int(text))
            elif op in _OPERATORS:
                query = getattr(query, op)(key, operand)
            elif op == key:
                # A bare value with no operator prefix is a mistake worth naming.
                raise SupabaseError(
                    f"filter for {key!r} needs an operator prefix, got {text!r}"
                )
            else:
                raise SupabaseError(f"unsupported filter {text!r} on {key!r}")

        return self._rows(query.execute())

    def insert(self, table: str, rows: list[dict] | dict) -> list[dict]:
        payload = rows if isinstance(rows, list) else [rows]
        return self._rows(self.client.table(table).insert(payload).execute())

    def insert_ignore_duplicates(self, table: str, rows: list[dict] | dict) -> list[dict]:
        """Insert, silently skipping rows that violate a unique constraint.

        This is the double-dispatch guard. Two ticks running at once (GitHub can
        overlap them, and the redundant crons make that likely) will both compute
        the same job; the database decides which one wins, the loser gets an
        empty list back and moves on.

        `default_to_null=False` is load-bearing: without it the client sends
        `null` for every omitted column, which would override the column defaults
        (`cancel_requested`, `created_at`, the generated `id`) and fail the NOT
        NULL check instead of taking the default.
        """
        payload = rows if isinstance(rows, list) else [rows]
        query = self.client.table(table).upsert(
            payload,
            ignore_duplicates=True,
            default_to_null=False,
        )
        return self._rows(query.execute())

    def update(self, table: str, match: dict, patch: dict) -> list[dict]:
        query = self.client.table(table).update(patch)
        for key, value in match.items():
            query = query.eq(key, value)
        return self._rows(query.execute())

    def rpc(self, fn: str, args: dict | None = None) -> object:
        """Call a Postgres function.

        Two matter here: ``km_claim_job`` atomically takes a job so two workers
        cannot run the same one, and ``km_get_account_secret`` is the only path
        to a decrypted Vault secret (Supabase exposes no REST endpoint for Vault
        — an RPC or a service-role query is the documented way in).
        """
        response = self.client.rpc(fn, args or {}).execute()
        return response.data

    # ---- Storage -------------------------------------------------------

    def storage_upload(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        """Upload, overwriting if the path already exists.

        `upsert: true` is required — the default is a `400 Asset Already Exists`,
        and re-pushing the same version path must be safe.
        """
        self.client.storage.from_(bucket).upload(
            path,
            data,
            {"content-type": content_type, "upsert": "true"},
        )

    def storage_download(self, bucket: str, path: str) -> bytes:
        return self.client.storage.from_(bucket).download(path)

    # ---- internals -----------------------------------------------------

    @staticmethod
    def _rows(response) -> list[dict]:
        data = getattr(response, "data", None)
        if data is None:
            return []
        return data if isinstance(data, list) else [data]


def _apply_order(query, spec: str):
    """``"version.desc"`` -> ``order("version", desc=True)``; bare name ascends.

    The column and direction are split only on the last dot so a schema-qualified
    name would still order by its final segment.
    """
    column, dot, direction = spec.rpartition(".")
    if dot and direction.lower() in {"asc", "desc"}:
        return query.order(column, desc=direction.lower() == "desc")
    return query.order(spec)