"""Directed multi-source multi-sink minimum cut.

The service implements its own 64-bit-capable max-flow / min-cut solver
(Dinic's blocking-flow algorithm).  Given a set of contamination source
areas, protected sink areas and a set of directed, non-reversible duct
segments with non-negative integer closing costs, it finds the minimum
total cost of segments whose removal makes every source unable to reach
every sink.

Among *all* minimum cuts, the unique solution whose source-side area set
is minimal under set inclusion is returned.  After Dinic terminates with
a maximum flow f, the set of nodes reachable from the super source in
the residual graph is exactly the intersection of the source sides of
every minimum cut, hence the inclusion-minimal source side.  Because the
residual graph is a property of the network (every max flow yields the
same reachable set for the intersection cut), the answer does not depend
on the order in which segments were submitted or on the internal flow
decomposition -- only on the input graph.

All capacities are plain Python ``int`` values, mathematically unbounded;
the solver additionally refuses any network whose totals cannot fit in a
signed 64-bit integer (see :func:`minimum_cut`), matching the required
64-bit integer capacity domain.
"""

from __future__ import annotations

from collections import deque
from typing import List, Sequence, Tuple

# Signed 64-bit integer range.  The problem guarantees costs <= 10**9 and
# at most 2000 segments, so the total can never exceed 2 * 10**12; the
# explicit bound documents and enforces the required capacity domain.
INT64_MAX = (1 << 63) - 1
COST_MAX = 1_000_000_000


class _Edge:
    """Mutable residual edge stored in a dense adjacency structure."""

    __slots__ = ("to", "rev", "cap")

    def __init__(self, to: int, rev: int, cap: int) -> None:
        self.to = to
        self.rev = rev
        self.cap = cap


class MinCutInputError(ValueError):
    """Raised when a network violates the solver's capacity contract."""


def _add_edge(graph: List[List[_Edge]], u: int, v: int, cap: int) -> None:
    """Add a directed edge u -> v with capacity ``cap``.

    A zero-capacity edge is added as such (no forward residual), because
    its presence still matters: once flow is pushed along a path using a
    parallel edge, reverse residual capacity can traverse it.
    """
    forward = _Edge(v, len(graph[v]), cap)
    backward = _Edge(u, len(graph[u]), 0)
    graph[u].append(forward)
    graph[v].append(backward)


def minimum_cut(
    area_ids: Sequence[str],
    segments: Sequence[Tuple[str, str, str, int]],
    source_ids: Sequence[str],
    sink_ids: Sequence[str],
) -> dict:
    """Compute the inclusion-minimal source side of a minimum cut.

    Parameters
    ----------
    area_ids:
        Every known area id (unique).
    segments:
        Iterable of ``(segment_id, from_area, to_area, cost)`` tuples.
        ``cost`` is an integer in ``[0, 10**9]``.  Self loops are legal
        but irrelevant to any s-t cut and are skipped; parallel edges are
        kept as separate segments (each can be closed independently).
    source_ids, sink_ids:
        Disjoint, non-empty collections of existing area ids.

    Returns
    -------
    dict
        ``{"source_side": [...], "cut_segments": [...], "total_cost": int}``
        with area and segment ids each sorted lexicographically ascending.
    """
    index = {area_id: i for i, area_id in enumerate(area_ids)}
    n = len(area_ids)
    super_source = n
    super_sink = n + 1
    node_count = n + 2

    total_real_cap = 0
    for seg_id, frm, to, cost in segments:
        if not isinstance(cost, int) or isinstance(cost, bool):
            raise MinCutInputError(f"segment {seg_id!r}: cost must be an integer")
        if not 0 <= cost <= COST_MAX:
            raise MinCutInputError(
                f"segment {seg_id!r}: cost {cost} outside [0, 10^9]"
            )
        if frm not in index or to not in index:
            raise MinCutInputError(f"segment {seg_id!r}: references unknown area")
        total_real_cap += cost
    if total_real_cap > INT64_MAX:
        raise MinCutInputError("network total capacity exceeds signed 64-bit range")

    missing_s = [s for s in source_ids if s not in index]
    missing_t = [t for t in sink_ids if t not in index]
    if missing_s or missing_t:
        raise MinCutInputError("source or sink references unknown area")
    if set(source_ids) & set(sink_ids):
        raise MinCutInputError("source and sink sets must be disjoint")
    if not source_ids or not sink_ids:
        raise MinCutInputError("source and sink sets must be non-empty")

    graph: List[List[_Edge]] = [[] for _ in range(node_count)]

    # Infinite-capacity links force every source onto the source side and
    # every sink onto the sink side of every finite min cut.  Any value
    # strictly larger than the sum of all real capacities is guaranteed to
    # never be saturated by a max flow; add one to stay strictly greater
    # even when the real total is zero.
    inf_cap = total_real_cap + 1
    if inf_cap > INT64_MAX:
        raise MinCutInputError("required auxiliary capacity exceeds signed 64-bit range")
    for s in source_ids:
        _add_edge(graph, super_source, index[s], inf_cap)
    for t in sink_ids:
        _add_edge(graph, index[t], super_sink, inf_cap)

    # Real edges: self loops can never cross an s-t cut (a node is on
    # exactly one side) and Dinic never needs them, so skip them.
    for seg_id, frm, to, cost in segments:
        u = index[frm]
        v = index[to]
        if u == v:
            continue
        _add_edge(graph, u, v, cost)

    flow = _dinic(graph, super_source, super_sink)
    if flow > INT64_MAX:
        raise MinCutInputError("max flow value exceeds signed 64-bit range")

    reachable = _residual_reachable(graph, super_source)

    # Super-source / super-sink never leave the model; the returned source
    # side contains real areas only.
    source_side = sorted(
        area_id for area_id, i in index.items() if i in reachable
    )
    source_set = set(source_side)

    # A segment crosses the cut exactly when its tail is source-side and
    # its head is sink-side.
    cut_segments = sorted(
        seg_id
        for seg_id, frm, to, _cost in segments
        if frm in source_set and to not in source_set
    )

    cost_by_id = {seg_id: cost for seg_id, _f, _t, cost in segments}
    total_cost = sum(cost_by_id[seg_id] for seg_id in cut_segments)

    return {
        "source_side": source_side,
        "cut_segments": cut_segments,
        "total_cost": total_cost,
    }


def _dinic(graph: List[List[_Edge]], s: int, t: int) -> int:
    """Run Dinic's algorithm; return the total max-flow value."""
    node_count = len(graph)
    level = [-1] * node_count
    # Current-arc pointers, rebuilt for every level graph.
    it = [0] * node_count
    total = 0

    while True:
        # BFS builds the level graph of edges that still carry capacity.
        level = [-1] * node_count
        level[s] = 0
        queue = deque([s])
        while queue:
            u = queue.popleft()
            for edge in graph[u]:
                if edge.cap > 0 and level[edge.to] < 0:
                    level[edge.to] = level[u] + 1
                    queue.append(edge.to)
        if level[t] < 0:
            return total

        it = [0] * node_count
        while True:
            pushed = _dfs(graph, level, it, s, t, INT64_MAX)
            if pushed == 0:
                break
            total += pushed
            if total > INT64_MAX:
                raise MinCutInputError(
                    "max flow value exceeds signed 64-bit range"
                )


def _dfs(
    graph: List[List[_Edge]],
    level: List[int],
    it: List[int],
    u: int,
    t: int,
    pushed: int,
) -> int:
    if u == t:
        return pushed
    while it[u] < len(graph[u]):
        edge = graph[u][it[u]]
        if edge.cap > 0 and level[edge.to] == level[u] + 1:
            sent = _dfs(graph, level, it, edge.to, t, min(pushed, edge.cap))
            if sent > 0:
                edge.cap -= sent
                graph[edge.to][edge.rev].cap += sent
                return sent
        it[u] += 1
    return 0


def _residual_reachable(graph: List[List[_Edge]], start: int) -> set:
    """BFS over strictly positive residual edges from ``start``."""
    seen = {start}
    queue = deque([start])
    while queue:
        u = queue.popleft()
        for edge in graph[u]:
            if edge.cap > 0 and edge.to not in seen:
                seen.add(edge.to)
                queue.append(edge.to)
    return seen
