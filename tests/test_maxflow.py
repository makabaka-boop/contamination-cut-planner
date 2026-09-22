"""Exhaustive cross-checks against a brute-force minimum cut.

For every network with at most 8 areas the tests enumerate *all* legal
source/sink partitions (non-empty, disjoint) and compare the
self-implemented solver with a brute-force enumeration of every valid
cut.  Several families of segment sets are exercised:

* a complete directed graph with small costs (forces many tied cuts);
* an all-zero complete graph (every cut is a zero-cost cut);
* seeded random graphs with random costs;
* random graphs with parallel edges and self loops.

The expensive complete-graph cases are capped at 6 areas while the
random families reach the required 8-area bound, keeping the suite fast
while still covering every legal partition for n <= 8 somewhere.
"""

from __future__ import annotations

import random

import pytest

from app.maxflow import minimum_cut
from tests.brute import all_valid_zones, brute_force_min_cut


def _complete_segments(areas, cost_fn):
    return [
        (f"e_{u}_{v}", u, v, cost_fn(u, v))
        for u in areas
        for v in areas
        if u != v
    ]


def _random_segments(areas, seed, density, cost_range, allow_parallel=False):
    rng = random.Random(seed)
    segments = []
    counter = 0
    for u in areas:
        for v in areas:
            if u == v:
                continue
            if rng.random() > density:
                continue
            segments.append((f"e{counter}", u, v, rng.randrange(*cost_range)))
            counter += 1
            if allow_parallel and rng.random() < 0.2:
                segments.append((f"e{counter}", u, v, rng.randrange(*cost_range)))
                counter += 1
        if allow_parallel and rng.random() < 0.1 and len(areas) > 1:
            segments.append((f"loop{counter}", u, u, rng.randrange(*cost_range)))
            counter += 1
    return segments


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6])
def test_complete_graph_small_costs_matches_brute_force(n):
    areas = [f"A{i}" for i in range(n)]
    rng = random.Random(100 + n)
    segments = _complete_segments(areas, lambda u, v: rng.randrange(0, 4))
    for sources, sinks in all_valid_zones(areas):
        expected = brute_force_min_cut(areas, segments, sources, sinks)
        actual = minimum_cut(areas, segments, sources, sinks)
        assert actual == expected


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6])
def test_all_zero_graph_every_cut_is_tied(n):
    areas = [f"Z{i}" for i in range(n)]
    segments = _complete_segments(areas, lambda u, v: 0)
    for sources, sinks in all_valid_zones(areas):
        expected = brute_force_min_cut(areas, segments, sources, sinks)
        actual = minimum_cut(areas, segments, sources, sinks)
        assert actual == expected
        # With all-zero capacities no source can push flow, so the
        # inclusion-minimal source side is exactly the source set.
        assert actual["source_side"] == sorted(sources)
        assert actual["total_cost"] == 0


@pytest.mark.parametrize("n", [5, 6, 7, 8])
def test_random_graphs_match_brute_force_all_partitions(n):
    areas = [f"R{n}_{i}" for i in range(n)]
    segments = _random_segments(areas, seed=700 + n, density=0.5, cost_range=(0, 6))
    for sources, sinks in all_valid_zones(areas):
        expected = brute_force_min_cut(areas, segments, sources, sinks)
        actual = minimum_cut(areas, segments, sources, sinks)
        assert actual == expected


@pytest.mark.parametrize("n", [6, 7, 8])
def test_random_graphs_large_costs_parallel_edges_all_partitions(n):
    areas = [f"P{n}_{i}" for i in range(n)]
    segments = _random_segments(
        areas,
        seed=1300 + n,
        density=0.6,
        cost_range=(0, 1_000_000),
        allow_parallel=True,
    )
    for sources, sinks in all_valid_zones(areas):
        expected = brute_force_min_cut(areas, segments, sources, sinks)
        actual = minimum_cut(areas, segments, sources, sinks)
        assert actual == expected


def test_solver_is_order_independent():
    """Submitting segments in another order must not change the answer."""
    areas = ["A", "B", "C", "D", "E"]
    rng = random.Random(9)
    segments = _random_segments(areas, seed=9, density=0.7, cost_range=(0, 50))
    sources = ("A",)
    sinks = ("D", "E")

    first = minimum_cut(areas, segments, sources, sinks)
    shuffled = segments[:]
    rng.shuffle(shuffled)
    second = minimum_cut(areas, shuffled, sources, sinks)
    assert first == second
    assert first["cut_segments"] == sorted(first["cut_segments"])
    assert first["source_side"] == sorted(first["source_side"])


def test_cheaper_of_two_serial_edges_is_the_cut():
    # s -> a -> t with edge costs 3 and 5: the minimum cut closes the
    # cheaper edge only.
    result = minimum_cut(
        ["s", "a", "t"],
        [("cheap", "s", "a", 3), ("pricey", "a", "t", 5)],
        ["s"],
        ["t"],
    )
    assert result == {
        "source_side": ["s"],
        "cut_segments": ["cheap"],
        "total_cost": 3,
    }


def test_parallel_edges_must_both_be_closed():
    # Two parallel directed segments between the same pair cannot be
    # distinguished by a node partition: both cross the cut together.
    result = minimum_cut(
        ["s", "t"],
        [("cheap", "s", "t", 3), ("pricey", "s", "t", 5)],
        ["s"],
        ["t"],
    )
    assert result == {
        "source_side": ["s"],
        "cut_segments": ["cheap", "pricey"],
        "total_cost": 8,
    }


def test_tied_cut_returns_inclusion_minimal_source_side():
    """Among equally cheap cuts, return the smallest source side.

    Network::

               1
        s ------------ t
        | 1            |          Two tied minimum cuts of value 2:
        a --1-- x --1-- b          * {e1, sa}: source side {s}
        | 1         1 |            * {e1, bt}: source side
        c ----1---- d                 {s,a,x,b,c,d}

    Every separating cut must close the direct edge e1 (cost 1); on the
    long route either sa or bt must go as well (cost 1).  The two tied
    source sides' intersection is {s}, itself a minimum cut side, so the
    inclusion-minimal answer closes {e1, sa}.
    """
    areas = ["s", "t", "a", "b", "c", "d", "x"]
    segments = [
        ("e1", "s", "t", 1),
        ("sa", "s", "a", 1),
        ("ax", "a", "x", 1),
        ("xb", "x", "b", 1),
        ("bt", "b", "t", 1),
        ("ac", "a", "c", 1),
        ("cd", "c", "d", 1),
        ("db", "d", "b", 1),
    ]
    expected = brute_force_min_cut(areas, segments, ("s",), ("t",))
    result = minimum_cut(areas, segments, ("s",), ("t",))
    assert result == expected
    assert result["total_cost"] == 2
    assert result["cut_segments"] == ["e1", "sa"]
    assert result["source_side"] == ["s"]


def test_multi_source_multi_sink_against_brute_force():
    areas = ["n0", "n1", "n2", "n3", "n4", "n5", "n6"]
    segments = _random_segments(areas, seed=4242, density=0.55, cost_range=(0, 9))
    result = minimum_cut(
        areas, segments, ("n0", "n1"), ("n5", "n6")
    )
    expected = brute_force_min_cut(
        areas, segments, ("n0", "n1"), ("n5", "n6")
    )
    assert result == expected


def test_no_path_gives_zero_cost_empty_cut_sources_only():
    result = minimum_cut(
        ["s", "x", "t"],
        [("sx", "s", "x", 7)],  # x -> t missing
        ["s"],
        ["t"],
    )
    assert result == {"source_side": ["s", "x"], "cut_segments": [], "total_cost": 0}


def test_zero_edges_appear_in_inclusion_minimal_tied_cut():
    # A free route s->m->t (both edges zero) parallel to a cost-5 direct
    # edge gives two tied minimum cuts of value 5:
    #   * source side {s}:    closes free1 (0) + paid (5)
    #   * source side {s,m}:  closes free2 (0) + paid (5)
    # The inclusion-minimal source side is {s}; the zero-cost edge
    # free1 is listed but adds nothing to the total.
    result = minimum_cut(
        ["s", "m", "t"],
        [("free1", "s", "m", 0), ("free2", "m", "t", 0), ("paid", "s", "t", 5)],
        ["s"],
        ["t"],
    )
    assert result["total_cost"] == 5
    assert result["cut_segments"] == ["free1", "paid"]
    assert result["source_side"] == ["s"]


def test_total_cost_exceeds_32_bits():
    # Per-edge costs are capped at 10^9 by the problem, so a >32-bit
    # total is reached by accumulating many max-cost edges.  5000 parallel
    # edges of 10^9 give 5*10^12 > 2^32-1, well inside signed 64-bit.
    areas = ["s", "t"]
    segments = [(f"big{i}", "s", "t", 1_000_000_000) for i in range(5000)]
    result2 = minimum_cut(areas, segments, ["s"], ["t"])
    assert result2["total_cost"] == 5_000_000_000_000
    assert result2["total_cost"] > (1 << 32) - 1
    assert len(result2["cut_segments"]) == 5000


def test_self_loops_never_cut():
    result = minimum_cut(
        ["s", "t"],
        [("loop", "s", "s", 1_000_000_000), ("edge", "s", "t", 1)],
        ["s"],
        ["t"],
    )
    assert result["cut_segments"] == ["edge"]
    assert result["total_cost"] == 1


def test_solution_actually_separates_every_source_sink_path():
    """Remove cut segments and verify no source reaches a sink."""
    areas = ["A", "B", "C", "D", "E"]
    segments = _random_segments(areas, seed=31337, density=0.75, cost_range=(1, 20))
    sources = ("A", "B")
    sinks = ("D", "E")
    result = minimum_cut(areas, segments, sources, sinks)
    cut = set(result["cut_segments"])
    adjacency: dict[str, list[str]] = {a: [] for a in areas}
    for sid, frm, to, _c in segments:
        if sid not in cut:
            adjacency[frm].append(to)
    seen = set(sources)
    stack = list(sources)
    while stack:
        u = stack.pop()
        for v in adjacency[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    assert not (seen & set(sinks))
    # Source side is exactly the reachable set in the cut graph.
    assert seen == set(result["source_side"])
