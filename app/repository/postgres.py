"""PostgreSQL repository backed by psycopg3 and a small connection pool."""

from __future__ import annotations

from typing import Any, Optional

import psycopg
from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

from .base import AdoptedRecord, ComputationRecord, PlanRecord

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id       TEXT PRIMARY KEY,
    version       INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    plan          JSONB NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS computations (
    computation_id UUID PRIMARY KEY,
    plan_id        TEXT NOT NULL REFERENCES plans(plan_id),
    content_hash   TEXT NOT NULL,
    result         JSONB NOT NULL,
    plan_snapshot  JSONB,
    plan_version   INTEGER,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (plan_id, content_hash)
);

ALTER TABLE computations ADD COLUMN IF NOT EXISTS plan_snapshot JSONB;
ALTER TABLE computations ADD COLUMN IF NOT EXISTS plan_version INTEGER;

CREATE TABLE IF NOT EXISTS adopted (
    singleton      INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    computation_id UUID NOT NULL REFERENCES computations(computation_id),
    adopted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    snapshot       JSONB NOT NULL
);
"""


class PostgresRepository:
    """Async PostgreSQL implementation of the repository protocol.

    Every mutating endpoint runs inside a single transaction.  The
    adoption transaction locks the referenced computation row first, so
    a snapshot swap either fully commits (new computation row + adopted
    row replaced together) or rolls back leaving the previous adopted
    result untouched.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "PostgresRepository":
        pool = AsyncConnectionPool(
            dsn,
            min_size=1,
            max_size=10,
            open=False,
            # Wait for the database container to finish starting instead
            # of crashing the API on first boot.
            check=AsyncConnectionPool.check_connection,
            kwargs={"autocommit": False, "row_factory": dict_row},
            timeout=30,
        )
        await pool.open(wait=True)
        repo = cls(pool)
        await repo.init_schema()
        return repo

    async def close(self) -> None:
        await self._pool.close()

    async def init_schema(self) -> None:
        async with self._pool.connection() as conn:
            # executescript runs the whole (multi-statement) DDL string.
            await conn.executescript(SCHEMA_SQL)
            await conn.commit()

    async def save_plan(
        self, plan_id: str, content_hash: str, plan: dict
    ) -> PlanRecord:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO plans (plan_id, version, content_hash, plan)
                    VALUES (%s, 1, %s, %s)
                    ON CONFLICT (plan_id) DO UPDATE
                        SET version = plans.version + 1,
                            content_hash = EXCLUDED.content_hash,
                            plan = EXCLUDED.plan,
                            updated_at = now()
                    """,
                    [plan_id, content_hash, Json(plan)],
                )
                row = await conn.execute(
                    "SELECT plan_id, version, content_hash, plan "
                    "FROM plans WHERE plan_id = %s",
                    [plan_id],
                )
                data = (await row.fetchone()) or {}
            return PlanRecord(
                plan_id=data["plan_id"],
                version=data["version"],
                content_hash=data["content_hash"],
                plan=data["plan"],
            )

    async def get_plan(self, plan_id: str) -> Optional[PlanRecord]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT plan_id, version, content_hash, plan "
                "FROM plans WHERE plan_id = %s",
                [plan_id],
            )
            data = await cur.fetchone()
        if data is None:
            return None
        return PlanRecord(
            plan_id=data["plan_id"],
            version=data["version"],
            content_hash=data["content_hash"],
            plan=data["plan"],
        )

    async def save_computation(
        self,
        computation_id: str,
        plan_id: str,
        content_hash: str,
        result: dict,
        plan: dict,
        plan_version: int,
    ) -> ComputationRecord:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO computations
                        (computation_id, plan_id, content_hash, result,
                         plan_snapshot, plan_version)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (plan_id, content_hash) DO NOTHING
                    """,
                    [
                        computation_id,
                        plan_id,
                        content_hash,
                        Json(result),
                        Json(plan),
                        plan_version,
                    ],
                )
                cur = await conn.execute(
                    """
                    SELECT computation_id, plan_id, content_hash, result,
                           plan_snapshot, plan_version
                    FROM computations
                    WHERE plan_id = %s AND content_hash = %s
                    """,
                    [plan_id, content_hash],
                )
                data = (await cur.fetchone()) or {}
        return ComputationRecord(
            computation_id=str(data["computation_id"]),
            plan_id=data["plan_id"],
            content_hash=data["content_hash"],
            result=data["result"],
            plan=data["plan_snapshot"],
            plan_version=data["plan_version"],
        )

    async def get_computation(
        self, computation_id: str
    ) -> Optional[ComputationRecord]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT computation_id, plan_id, content_hash, result,
                       plan_snapshot, plan_version
                FROM computations WHERE computation_id = %s
                """,
                [computation_id],
            )
            data = await cur.fetchone()
        if data is None:
            return None
        return ComputationRecord(
            computation_id=str(data["computation_id"]),
            plan_id=data["plan_id"],
            content_hash=data["content_hash"],
            result=data["result"],
            plan=data["plan_snapshot"],
            plan_version=data["plan_version"],
        )

    async def adopt(
        self, computation_id: str, adopted_at: str, snapshot: dict
    ) -> Optional[AdoptedRecord]:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                # Locking the computation row first ensures the referenced
                # result exists and stays valid for the whole snapshot swap.
                cur = await conn.execute(
                    "SELECT computation_id FROM computations "
                    "WHERE computation_id = %s FOR UPDATE",
                    [computation_id],
                )
                if await cur.fetchone() is None:
                    # Rollback is caught by the transaction context: the
                    # transaction aborts cleanly and code resumes after
                    # the block, leaving any prior adopted row untouched.
                    await conn.rollback()
                    return None
                await conn.execute(
                    """
                    INSERT INTO adopted
                        (singleton, computation_id, adopted_at, snapshot)
                    VALUES (1, %s, %s, %s)
                    ON CONFLICT (singleton) DO UPDATE
                        SET computation_id = EXCLUDED.computation_id,
                            adopted_at = EXCLUDED.adopted_at,
                            snapshot = EXCLUDED.snapshot
                    """,
                    [computation_id, adopted_at, Json(snapshot)],
                )
                cur = await conn.execute(
                    """
                    SELECT a.computation_id, a.adopted_at, a.snapshot
                    FROM adopted a WHERE a.singleton = 1
                    """
                )
                data = await cur.fetchone()
        return AdoptedRecord(
            computation_id=str(data["computation_id"]),
            adopted_at=data["adopted_at"].isoformat(),
            snapshot=data["snapshot"],
        )

    async def get_adopted(self) -> Optional[AdoptedRecord]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT computation_id, adopted_at, snapshot "
                "FROM adopted WHERE singleton = 1"
            )
            data = await cur.fetchone()
        if data is None:
            return None
        return AdoptedRecord(
            computation_id=str(data["computation_id"]),
            adopted_at=data["adopted_at"].isoformat(),
            snapshot=data["snapshot"],
        )


def Json(value: Any) -> psycopg.types.json.Json:
    return psycopg.types.json.Json(value)
