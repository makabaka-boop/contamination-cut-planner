"""Service layer: canonical hashing, minimum-cut computation, snapshots."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import UUID, uuid4

from .maxflow import MinCutInputError, minimum_cut
from .repository.base import (
    AdoptedRecord,
    ComputationRecord,
    PlanRecord,
    Repository,
)
from .validation import validate_plan

PLAN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class ServiceError(Exception):
    """An application-level failure with a stable, documented code."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def canonical_hash(plan: dict) -> str:
    """Stable SHA-256 over the canonical JSON encoding of a plan."""
    payload = json.dumps(
        plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_result(plan: dict) -> dict:
    """Run the self-implemented max-flow/min-cut solver on a valid plan."""
    segments = [
        (s["id"], s["from"], s["to"], s["cost"]) for s in plan["segments"]
    ]
    try:
        return minimum_cut(
            area_ids=plan["areas"],
            segments=segments,
            source_ids=plan["sources"],
            sink_ids=plan["sinks"],
        )
    except MinCutInputError as exc:
        # Defensive: validation already covers these cases.  Returning a
        # stable code means a failed computation never produces a stored
        # (or adopted) result.
        raise ServiceError(422, "COMPUTATION_FAILED", str(exc)) from exc


class PlanService:
    def __init__(self, repository: Repository) -> None:
        self._repo = repository

    @staticmethod
    def check_plan_id(plan_id: str) -> None:
        if not PLAN_ID_RE.fullmatch(plan_id):
            raise ServiceError(
                400,
                "INVALID_ID",
                f"plan id must match [A-Za-z0-9_-]{{1,32}}: {plan_id!r}",
            )

    async def save_plan(self, plan_id: str, raw_doc: dict) -> PlanRecord:
        self.check_plan_id(plan_id)
        # Validation happens entirely before any persistence call, so an
        # illegal full replacement cannot touch the stored plan.
        plan = validate_plan(raw_doc)
        content_hash = canonical_hash(plan)
        return await self._repo.save_plan(plan_id, content_hash, plan)

    async def get_plan(self, plan_id: str) -> PlanRecord:
        self.check_plan_id(plan_id)
        record = await self._repo.get_plan(plan_id)
        if record is None:
            raise ServiceError(404, "PLAN_NOT_FOUND", f"no such plan: {plan_id}")
        return record

    async def compute(self, plan_id: str) -> dict:
        self.check_plan_id(plan_id)
        record = await self._repo.get_plan(plan_id)
        if record is None:
            raise ServiceError(404, "PLAN_NOT_FOUND", f"no such plan: {plan_id}")

        result = compute_result(record.plan)
        computation_id = str(uuid4())
        stored = await self._repo.save_computation(
            computation_id=computation_id,
            plan_id=plan_id,
            content_hash=record.content_hash,
            result=result,
            plan=record.plan,
            plan_version=record.version,
        )
        return {
            "computation_id": stored.computation_id,
            "plan_id": plan_id,
            "content_hash": record.content_hash,
            "result": result,
        }

    async def adopt(self, computation_id: str) -> AdoptedRecord:
        # Validate the id shape cheaply; the authoritative existence
        # check is the transactional repository call.
        try:
            parsed = UUID(computation_id)
        except (ValueError, AttributeError) as exc:
            raise ServiceError(
                404, "RESULT_NOT_FOUND", "no such computation result"
            ) from exc
        if parsed.version != 4:
            raise ServiceError(
                404, "RESULT_NOT_FOUND", "no such computation result"
            )

        computation = await self._repo.get_computation(str(parsed))
        if computation is None:
            raise ServiceError(
                404, "RESULT_NOT_FOUND", "no such computation result"
            )

        # The snapshot is built entirely from the frozen computation
        # record, so later edits to the plan (even version bumps) can
        # never alter what gets adopted from an earlier result.
        snapshot = build_snapshot(computation)
        adopted_at = datetime.now(timezone.utc).isoformat()
        adopted = await self._repo.adopt(
            computation_id=computation.computation_id,
            adopted_at=adopted_at,
            snapshot=snapshot,
        )
        if adopted is None:
            # The computation vanished between lookup and the atomic
            # swap; the previously adopted result stays untouched.
            raise ServiceError(
                404, "RESULT_NOT_FOUND", "no such computation result"
            )
        return adopted

    async def get_adopted(self) -> AdoptedRecord:
        adopted = await self._repo.get_adopted()
        if adopted is None:
            raise ServiceError(
                404, "ADOPTED_RESULT_NOT_FOUND", "no result has been adopted yet"
            )
        return adopted


def build_snapshot(computation: ComputationRecord) -> dict:
    """Freeze everything needed to execute the closing list later."""
    return {
        "plan_id": computation.plan_id,
        "plan_version": computation.plan_version,
        "content_hash": computation.content_hash,
        "computation_id": computation.computation_id,
        "plan": copy.deepcopy(computation.plan),
        "result": copy.deepcopy(computation.result),
    }
