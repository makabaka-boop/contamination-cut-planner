"""Repository protocol shared by the in-memory and PostgreSQL backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class PlanRecord:
    plan_id: str
    version: int
    content_hash: str
    plan: dict


@dataclass
class ComputationRecord:
    computation_id: str
    plan_id: str
    content_hash: str
    result: dict  # {"source_side": [...], "cut_segments": [...], "total_cost": int}
    # The exact, canonical plan document this result was computed from.
    # Freezing it here keeps an adoption snapshot valid even after the
    # plan itself is replaced by a newer version.
    plan: dict
    plan_version: int


@dataclass
class AdoptedRecord:
    computation_id: str
    adopted_at: str  # ISO-8601 UTC timestamp produced by the database
    snapshot: dict


class Repository(Protocol):
    async def close(self) -> None: ...

    async def save_plan(
        self, plan_id: str, content_hash: str, plan: dict
    ) -> PlanRecord:
        """Create version 1 or bump the version of an existing plan.

        The whole upsert is atomic: a rejected document never reaches the
        database and a stored plan is never half-overwritten.
        """
        ...

    async def get_plan(self, plan_id: str) -> Optional[PlanRecord]: ...

    async def save_computation(
        self,
        computation_id: str,
        plan_id: str,
        content_hash: str,
        result: dict,
        plan: dict,
        plan_version: int,
    ) -> ComputationRecord:
        """Store a successful computation; idempotent on (plan_id, hash).

        The canonical plan document and its version at computation time
        are frozen with the result.
        """
        ...

    async def get_computation(
        self, computation_id: str
    ) -> Optional[ComputationRecord]: ...

    async def adopt(
        self, computation_id: str, adopted_at: str, snapshot: dict
    ) -> Optional[AdoptedRecord]:
        """Replace the adopted snapshot atomically.

        Returns the adopted record, or ``None`` when the computation id
        does not reference a stored successful computation.  On ``None``
        neither the plan nor any previously adopted snapshot changes.
        """
        ...

    async def get_adopted(self) -> Optional[AdoptedRecord]: ...
