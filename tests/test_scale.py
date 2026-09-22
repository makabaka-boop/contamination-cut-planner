"""Scale tests at the problem's declared maximums."""

from __future__ import annotations

import random
import time

from app.maxflow import minimum_cut


def test_maximum_scale_300_areas_2000_segments_runs_fast():
    rng = random.Random(20260922)
    n = 300
    areas = [f"area_{i:03d}" for i in range(n)]
    segments = []
    seen_pairs = set()
    for i in range(2000):
        while True:
            u = rng.randrange(n)
            v = rng.randrange(n)
            if u != v and (u, v) not in seen_pairs:
                seen_pairs.add((u, v))
                break
        segments.append((f"seg_{i:04d}", areas[u], areas[v], rng.randrange(0, 1_000_000_001)))
    sources = [areas[0], areas[1], areas[2]]
    sinks = [areas[n - 1], areas[n - 2], areas[n - 3]]

    start = time.perf_counter()
    result = minimum_cut(areas, segments, sources, sinks)
    elapsed = time.perf_counter() - start

    assert elapsed < 5.0, f"solver too slow at max scale: {elapsed:.2f}s"
    assert result["cut_segments"] == sorted(result["cut_segments"])
    assert result["source_side"] == sorted(result["source_side"])
    assert 0 <= result["total_cost"] <= 2 * 10**12

    # The reported list must actually disconnect every source from every sink.
    cut = set(result["cut_segments"])
    adj = {a: [] for a in areas}
    for sid, frm, to, _c in segments:
        if sid not in cut:
            adj[frm].append(to)
    seen = set(sources)
    stack = list(sources)
    while stack:
        u = stack.pop()
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    assert not (seen & set(sinks))

    # Sources themselves must always be on the source side; sinks never.
    assert set(sources) <= set(result["source_side"])
    assert not (set(result["source_side"]) & set(sinks))


def test_max_scale_dense_parallel_edges():
    # 2000 segments concentrated on few nodes stresses parallel edges:
    # ~2000 parallel positive edges between s and t plus a zero chain
    # via a.  The zero chain cannot be avoided, so max flow equals the
    # sum of every positive edge and the inclusion-minimal cut closes
    # all of them plus the zero edge out of s.
    areas = ["s", "a", "t"]
    rng = random.Random(7)
    segments = [("z0", "s", "a", 0), ("z1", "a", "t", 0)]
    for i in range(1998):
        segments.append((f"s{i}", "s", "t", rng.randrange(1, 1_000_000_001)))
    result = minimum_cut(areas, segments, ["s"], ["t"])
    expected_total = sum(c for _sid, _f, _t, c in segments[2:])
    assert result["total_cost"] == expected_total
    assert "z0" in result["cut_segments"]
    assert all(sid in result["cut_segments"] for sid, *_ in segments[2:])
