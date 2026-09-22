"""Repository semantics shared by any backend implementation.

These run against the in-memory backend; the PostgreSQL backend
satisfies the same protocol (same transactions, same row constraints).
"""

from __future__ import annotations

import asyncio

import pytest

from app.repository import InMemoryRepository


@pytest.mark.asyncio
async def test_plan_versions_are_monotonic():
    repo = InMemoryRepository()
    plan = {"areas": ["a", "b"], "segments": [], "sources": ["a"], "sinks": ["b"]}
    r1 = await repo.save_plan("p", "h1", plan)
    r2 = await repo.save_plan("p", "h2", plan)
    r3 = await repo.save_plan("p", "h2", plan)
    assert (r1.version, r2.version, r3.version) == (1, 2, 3)
    assert (await repo.get_plan("p")).version == 3
    assert (await repo.get_plan("missing")) is None


@pytest.mark.asyncio
async def test_computation_deduplicated_by_content():
    repo = InMemoryRepository()
    plan = {"areas": ["a", "b"], "segments": [], "sources": ["a"], "sinks": ["b"]}
    await repo.save_plan("p", "h1", plan)
    result = {"source_side": ["a"], "cut_segments": [], "total_cost": 0}
    c1 = await repo.save_computation("11111111-1111-4111-8111-111111111111",
                                     "p", "h1", result, plan, 1)
    c2 = await repo.save_computation("22222222-2222-4222-8222-222222222222",
                                     "p", "h1", result, plan, 1)
    assert c1.computation_id == c2.computation_id
    assert (await repo.get_computation("22222222-2222-4222-8222-222222222222")) is None


@pytest.mark.asyncio
async def test_computation_freezes_plan_version():
    repo = InMemoryRepository()
    v1 = {"areas": ["a", "b"], "segments": [], "sources": ["a"], "sinks": ["b"]}
    await repo.save_plan("p", "h1", v1)
    result = {"source_side": ["a"], "cut_segments": [], "total_cost": 0}
    stored = await repo.save_computation(
        "11111111-1111-4111-8111-111111111111", "p", "h1", result, v1, 1
    )
    # Plan moves on; the computation record still carries version 1.
    await repo.save_plan("p", "h2", {**v1, "sinks": ["b"]})
    assert stored.plan_version == 1
    assert stored.plan == v1


@pytest.mark.asyncio
async def test_adopt_missing_computation_changes_nothing():
    repo = InMemoryRepository()
    out = await repo.adopt("00000000-0000-4000-8000-000000000000", "now", {})
    assert out is None
    assert await repo.get_adopted() is None


@pytest.mark.asyncio
async def test_adopt_replaces_snapshot_and_gets_it_back():
    repo = InMemoryRepository()
    plan = {"areas": ["a", "b"], "segments": [], "sources": ["a"], "sinks": ["b"]}
    await repo.save_plan("p", "h1", plan)
    result = {"source_side": ["a"], "cut_segments": [], "total_cost": 0}
    cid1 = "11111111-1111-4111-8111-111111111111"
    cid2 = "22222222-2222-4222-8222-222222222222"
    await repo.save_computation(cid1, "p", "h1", result, plan, 1)
    await repo.save_computation(cid2, "p", "h2", result, plan, 1)
    a1 = await repo.adopt(cid1, "t1", {"computation_id": cid1})
    assert (await repo.get_adopted()).computation_id == cid1
    a2 = await repo.adopt(cid2, "t2", {"computation_id": cid2})
    assert a2.computation_id == cid2
    assert (await repo.get_adopted()).snapshot == {"computation_id": cid2}


@pytest.mark.asyncio
async def test_concurrent_plan_saves_keep_monotonic_versions():
    repo = InMemoryRepository()
    plan = {"areas": ["a", "b"], "segments": [], "sources": ["a"], "sinks": ["b"]}

    async def save(i):
        await repo.save_plan("p", f"h{i}", plan)

    await asyncio.gather(*(save(i) for i in range(20)))
    assert (await repo.get_plan("p")).version == 20
