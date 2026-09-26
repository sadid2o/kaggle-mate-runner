"""Push-copy builder.

Kaggle runs whatever is in the folder we push — so that folder is where the
watchdog goes in, and nowhere else. The user's stored version stays byte-exact
in Supabase Storage, which is what makes "the original is never modified" a
statement about the system rather than a promise about our care.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import watchdog


@dataclass(frozen=True)
class PushOverrides:
    """Per-schedule tweaks applied on top of the pulled metadata.

    Only fields Kaggle documents are accepted here; anything unknown is left
    to the metadata file rather than invented.
    """

    enable_gpu: bool | None = None
    enable_tpu: bool | None = None
    enable_internet: bool | None = None
    machine_shape: str | None = None

    def apply(self, meta: dict) -> dict:
        out = dict(meta)
        for field in ("enable_gpu", "enable_tpu", "enable_internet"):
            value = getattr(self, field)
            if value is not None:
                out[field] = "true" if value else "false"
        if self.machine_shape is not None:
            out["machine_shape"] = self.machine_shape
        return out


@dataclass(frozen=True)
class PushPlan:
    folder: Path
    code_file: str
    language: str
    watchdog_injected: bool


def build_push_folder(
    dest: Path,
    code_bytes: bytes,
    code_file: str,
    metadata: dict,
    deadline_utc: datetime,
    overrides: PushOverrides | None = None,
    cancel: watchdog.CancelChannel | None = None,
) -> PushPlan:
    """Write a self-contained folder ready for ``kaggle kernels push``.

    The metadata's ``code_file`` is trusted to name the file we just wrote —
    Kaggle resolves the file by that name, so writing anything else would fail
    there instead of here.

    ``cancel`` is passed only when the notebook will have internet; without it
    the watchdog still enforces the deadline, but an early "Cancel now" cannot
    reach the run. That distinction is reported by the caller, not hidden.
    """
    dest.mkdir(parents=True, exist_ok=True)

    language = str(metadata.get("language", "python")).lower()
    injected = watchdog.inject(code_bytes, code_file, language, deadline_utc, cancel)

    final_bytes = injected if injected is not None else code_bytes
    (dest / code_file).write_bytes(final_bytes)

    meta = (overrides or PushOverrides()).apply(metadata)
    meta["code_file"] = code_file
    (dest / "kernel-metadata.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return PushPlan(
        folder=dest,
        code_file=code_file,
        language=language,
        watchdog_injected=injected is not None,
    )


def parse_metadata(raw: bytes) -> dict:
    """Parse a kernel-metadata.json, tolerating the stringy booleans Kaggle emits.

    The CLI writes ``"enable_gpu": "false"`` (a string) far more often than a
    real boolean, so anything downstream that does ``if meta['enable_gpu']``
    would be wrong. Normalising once, here, keeps that trap in one place.
    """
    meta = json.loads(raw)
    for field in ("enable_gpu", "enable_tpu", "enable_internet", "is_private"):
        if field in meta and isinstance(meta[field], str):
            meta[field] = meta[field].strip().lower() == "true"
    return meta