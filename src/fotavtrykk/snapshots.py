"""Snapshot load/save.

A run's envelope file *is* the snapshot. Observation history rides inside each
claim's qualifiers (`first_observed_at` / `last_observed_at`), so there is no
separate history database to drift out of sync with the output.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import Envelope


def load_envelopes(path: Path) -> dict[str, Envelope]:
    """Read a previous run. Unparseable lines are skipped rather than fatal:
    a corrupt prior snapshot must not stop today's batch from completing."""
    if not path or not path.exists():
        return {}
    envelopes: dict[str, Envelope] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            envelope = Envelope.model_validate_json(line)
        except Exception:  # noqa: BLE001 - tolerate a damaged prior snapshot
            continue
        envelopes[envelope.organisation_number] = envelope
    return envelopes


def write_envelopes(path: Path, envelopes: list[Envelope]) -> str:
    """Write envelopes and return the SHA-256 of the file's contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(e.model_dump_json(exclude_none=False) + "\n" for e in envelopes)
    path.write_text(payload, encoding="utf-8")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def manifest(envelopes: list[Envelope], *, run_id: str, content_sha256: str) -> dict:
    return {
        "run_id": run_id,
        "organisations": [e.organisation_number for e in envelopes],
        "count": len(envelopes),
        "content_sha256": content_sha256,
    }


def write_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
