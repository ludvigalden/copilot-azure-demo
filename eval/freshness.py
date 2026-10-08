"""Age-only classification after all existing release evidence checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import gate


class State(StrEnum):
    CURRENT = "CURRENT"
    EXPIRED_VALID = "EXPIRED_VALID"
    MALFORMED = "MALFORMED"
    FUTURE = "FUTURE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Classification:
    state: State
    failures: tuple[str, ...] = ()


def timestamps(document: dict[str, Any]) -> tuple[datetime, datetime, datetime]:
    evidence = document["release_evidence"]
    values = tuple(
        datetime.fromisoformat(evidence[key])
        for key in ("started_at", "completed_at", "expires_at")
    )
    if any(value.tzinfo is None for value in values):
        raise ValueError("timezone required")
    started, completed, expires = values
    if not started <= completed < expires:
        raise ValueError("invalid evaluation interval")
    return started, completed, expires


def validate_non_age(document: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Evaluate the unchanged strict gate at completion, not at approval time."""
    _, completed, _ = timestamps(document)
    verdict = gate.gate_release_document(document, candidate, completed)
    if not verdict.passed or len(gate.load_golden_dataset().questions) != 15:
        raise ValueError("invalid non-age release evidence")


def classify(
    document: dict[str, Any], candidate: dict[str, Any], now: datetime | None = None
) -> Classification:
    clock = now or datetime.now(UTC)
    try:
        started, completed, expires = timestamps(document)
    except (KeyError, TypeError, ValueError, AttributeError):
        return Classification(State.MALFORMED, ("invalid evaluation timestamps",))
    if clock.tzinfo is None or started > clock or completed > clock:
        return Classification(State.FUTURE, ("future evaluation",))
    try:
        validate_non_age(document, candidate)
    except (ValueError, KeyError, TypeError, AttributeError):
        return Classification(State.FAILED, ("non-age evaluation gate rejected",))
    if clock >= expires or (clock - completed).total_seconds() > 7200:
        return Classification(State.EXPIRED_VALID)
    return Classification(State.CURRENT)
