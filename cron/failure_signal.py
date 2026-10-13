"""Machine-readable open-failure signal for external watchdogs.

``last_status``/``failure_streak`` live inside ``jobs.json``, which no watchdog parses, so a
dead job stayed silent for days. Instead each OPEN failure is one file,
``<cron store>/failures/<job_id>.json`` (schema ``cron-failure-signal/1``): written by
``mark_job_run`` when a run fails and no unreachable-model re-run is parked, deleted by the
next green run. Existence == open failure; latest failure wins.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_time import now as _hermes_now
from utils import atomic_write_text

logger = logging.getLogger(__name__)

SCHEMA = "cron-failure-signal/1"


def signal_path_for(job_id: str) -> Path:
    from cron.jobs import _current_cron_store  # late: cron.jobs imports this module lazily

    return _current_cron_store().cron_dir / "failures" / f"{job_id}.json"


def _signal_payload(job: Dict[str, Any], error_text: Optional[str], kind: str,
                    ladder_attempt: int) -> Dict[str, Any]:
    from cron.incidents import _error_signature, _redact_error
    from cron.jobs import _current_cron_store
    from cron.unreachable_retry import RETRY_DELAYS_SECONDS
    from hermes_constants import profile_name_for_home

    job_id = job["id"]
    delays = list(RETRY_DELAYS_SECONDS)
    return {
        "schema": SCHEMA,
        "job_id": job_id,
        "job_name": job.get("name") or job_id,
        "profile": profile_name_for_home(_current_cron_store().cron_dir.parent) or "default",
        # last_run_at, not the sticky last_failure: on a delivery_failed run last_failure
        # still names an older agent failure.
        "at": job.get("last_run_at") or _hermes_now().isoformat(),
        "kind": kind,
        "error": _redact_error(error_text) if error_text else None,
        "error_sig": _error_signature(job_id, error_text) if error_text else None,
        "failure_streak": int(job.get("failure_streak") or 0),
        "next_run_at": job.get("next_run_at"),
        "retry_ladder": ({"attempt": ladder_attempt, "delays_seconds": delays}
                         if ladder_attempt >= len(delays) else None),
    }


def write_failure_signal(job: Dict[str, Any], error_text: Optional[str], *, kind: str,
                         ladder_attempt: int = 0) -> None:
    """Best-effort: never raises into the scheduler. ``ladder_attempt``: re-run rungs this
    failure had already spent; recorded only once the ladder is exhausted."""
    try:
        # atomic_write_text creates failures/ itself (never resurrecting a deleted profile home).
        payload = _signal_payload(job, error_text, kind, ladder_attempt)
        atomic_write_text(signal_path_for(job["id"]), json.dumps(payload, indent=2) + "\n",
                          tmp_prefix=".failure_", mode=0o600)
    except Exception:  # best-effort: a signal must never break run accounting
        logger.debug("Failed to write failure signal for job %r", job.get("id"), exc_info=True)


def clear_failure_signal(job_id: str) -> None:
    try:
        signal_path_for(job_id).unlink(missing_ok=True)
    except OSError as e:
        logger.debug("Failed to clear failure signal for job %r: %s", job_id, e)


def sync_after_run(job: Dict[str, Any], *, ladder_attempt: int) -> None:
    """Called by ``mark_job_run`` once the run outcome and ladder state are final: a green run
    closes the open failure; any other status opens/overwrites it, unless an unreachable-model
    re-run is parked (``plan_retry`` re-adds its state only when it parked one; the interim
    rung stays silent like the failure notice)."""
    from cron.unreachable_retry import STATE_KEY

    kind = job.get("last_status")
    if kind == "ok":
        clear_failure_signal(job["id"])
    elif not job.get(STATE_KEY):
        write_failure_signal(job, job.get("last_error") or job.get("last_delivery_error"),
                             kind=kind, ladder_attempt=ladder_attempt)
