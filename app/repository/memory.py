"""Deterministic in-memory repository used for tests and local runs."""

from __future__ import annotations

import threading
from typing import Optional

from .base import AdoptedRecord, ComputationRecord, PlanRecord


class InMemoryRepository:
    """Thread-safe in-process implementation of the repository protocol.

    Locking mirrors the row-level guarantees of the PostgreSQL backend so
    that behavior under concurrency is the same in tests and in
    production: plan versions are monotonic, computations are
    deduplicated by content, and adoption is an all-or-nothing swap.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._plans: dict[str, PlanRecord] = {}
        self._computations: dict[str, ComputationRecord] = {}
        self._adopted: Optional[AdoptedRecord] = None

    async def close(self) -> None:
        return None

    async def save_plan(
        self, plan_id: str, content_hash: str, plan: dict
    ) -> PlanRecord:
        with self._lock:
            existing = self._plans.get(plan_id)
            if existing is None:
                record = PlanRecord(
                    plan_id=plan_id,
                    version=1,
                    content_hash=content_hash,
                    plan=plan,
                )
            else:
                record = PlanRecord(
                    plan_id=plan_id,
                    version=existing.version + 1,
                    content_hash=content_hash,
                    plan=plan,
                )
            self._plans[plan_id] = record
            return record

    async def get_plan(self, plan_id: str) -> Optional[PlanRecord]:
        with self._lock:
            return self._plans.get(plan_id)

    async def save_computation(
        self,
        computation_id: str,
        plan_id: str,
        content_hash: str,
        result: dict,
        plan: dict,
        plan_version: int,
    ) -> ComputationRecord:
        with self._lock:
            # Deduplicate on plan content: recomputing an unchanged plan
            # reuses the same computation id, so review checklists stay
            # stable across restarts and repeated calls.
            for existing in self._computations.values():
                if (
                    existing.plan_id == plan_id
                    and existing.content_hash == content_hash
                ):
                    return existing
            record = ComputationRecord(
                computation_id=computation_id,
                plan_id=plan_id,
                content_hash=content_hash,
                result=result,
                plan=plan,
                plan_version=plan_version,
            )
            self._computations[computation_id] = record
            return record

    async def get_computation(
        self, computation_id: str
    ) -> Optional[ComputationRecord]:
        with self._lock:
            return self._computations.get(computation_id)

    async def adopt(
        self, computation_id: str, adopted_at: str, snapshot: dict
    ) -> Optional[AdoptedRecord]:
        with self._lock:
            if computation_id not in self._computations:
                return None
            record = AdoptedRecord(
                computation_id=computation_id,
                adopted_at=adopted_at,
                snapshot=snapshot,
            )
            self._adopted = record
            return record

    async def get_adopted(self) -> Optional[AdoptedRecord]:
        with self._lock:
            return self._adopted
