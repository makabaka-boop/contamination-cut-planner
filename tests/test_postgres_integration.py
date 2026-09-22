"""PostgreSQL integration test.

Runs only when the TEST_DATABASE_URL environment variable points at a
reachable PostgreSQL database (the docker-compose service provides one).
With Docker:

    docker compose up -d db
    TEST_DATABASE_URL=postgresql://cleanroom:cleanroom@localhost:5432/cleanroom \
        python -m pytest tests/test_postgres_integration.py

It verifies the durability requirements: plans, computations and the
adopted snapshot survive a repository shutdown/reopen (the API
container restarting), and content hashes keep computation ids stable.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from app.repository.postgres import PostgresRepository  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


PLAN = {
    "areas": ["s", "a", "t"],
    "segments": [
        {"id": "e1", "from": "s", "to": "a", "cost": 1},
        {"id": "e2", "from": "a", "to": "t", "cost": 2},
    ],
    "sources": ["s"],
    "sinks": ["t"],
}
RESULT = {"source_side": ["s"], "cut_segments": ["e1"], "total_cost": 1}


@pytest.mark.asyncio
async def test_state_survives_reopen_and_order_is_stable():
    repo = await PostgresRepository.connect(DSN)
    try:
        # Start from a known-clean slate.
        async with repo._pool.connection() as conn:
            await conn.execute("TRUNCATE adopted, computations, plans")
            await conn.commit()

        saved = await repo.save_plan("durability-1", "hash-v1", PLAN)
        assert saved.version == 1
        comp = await repo.save_computation(
            "11111111-1111-4111-8111-111111111111",
            "durability-1", "hash-v1", RESULT, PLAN, 1,
        )
        assert comp.computation_id == "11111111-1111-4111-8111-111111111111"
        adopted = await repo.adopt(
            comp.computation_id, "2026-09-22T00:00:00+00:00",
            {
                "plan_id": "durability-1",
                "plan_version": 1,
                "content_hash": "hash-v1",
                "computation_id": comp.computation_id,
                "plan": PLAN,
                "result": RESULT,
            },
        )
        assert adopted is not None
    finally:
        await repo.close()

    # Simulate an API/container restart: new pool against the same
    # database files (the pgdata volume).
    repo2 = await PostgresRepository.connect(DSN)
    try:
        again = await repo2.get_plan("durability-1")
        assert again is not None
        assert again.version == 1 and again.content_hash == "hash-v1"
        assert again.plan == PLAN

        same = await repo2.save_computation(
            "22222222-2222-4222-8222-222222222222",
            "durability-1", "hash-v1", RESULT, PLAN, 1,
        )
        # Same content hash -> same stable computation id, even after restart.
        assert same.computation_id == "11111111-1111-4111-8111-111111111111"

        adopted_after = await repo2.get_adopted()
        assert adopted_after is not None
        assert adopted_after.computation_id == "11111111-1111-4111-8111-111111111111"
        assert adopted_after.snapshot["result"]["total_cost"] == 1
        assert adopted_after.snapshot["plan"] == PLAN

        # Adopting a nonexistent result must leave the snapshot in place.
        missing = await repo2.adopt(
            "33333333-3333-4333-8333-333333333333",
            "2026-09-22T01:00:00+00:00", {},
        )
        assert missing is None
        still = await repo2.get_adopted()
        assert still.computation_id == "11111111-1111-4111-8111-111111111111"
    finally:
        await repo2.close()
