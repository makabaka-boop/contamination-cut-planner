"""Reference brute-force minimum cut used to cross-check the solver.

For every valid partition of the area set into a source side (contains
all contamination sources, excludes all protected sinks) and a sink
side, we sum the costs of segments crossing forward.  The minimum such
sum is the minimum-cut value; among partitions attaining it, the
intersection of the source sides is the inclusion-minimal unique answer
required by the specification.
"""

from __future__ import annotations

from typing import Iterable, Sequence


def brute_force_min_cut(
    area_ids: Sequence[str],
    segments: Sequence[tuple[str, str, str, int]],
    source_ids: Sequence[str],
    sink_ids: Sequence[str],
) -> dict:
    forced_in = set(source_ids)
    forced_out = set(sink_ids)
    free = sorted(set(area_ids) - forced_in - forced_out)

    best_value: int | None = None
    best_source_sides: list[frozenset[str]] = []

    # Enumerate every subset of the free areas (2**|free| partitions).
    for mask in range(1 << len(free)):
        side = set(forced_in)
        for i, area in enumerate(free):
            if mask & (1 << i):
                side.add(area)
        value = sum(
            c for _sid, frm, to, c in segments if frm in side and to not in side
        )
        fs = frozenset(side)
        if best_value is None or value < best_value:
            best_value = value
            best_source_sides = [fs]
        elif value == best_value:
            best_source_sides.append(fs)

    minimal_side = frozenset.intersection(*best_source_sides)
    cut_ids = sorted(
        sid
        for sid, frm, to, _c in segments
        if frm in minimal_side and to not in minimal_side
    )
    cut_id_set = set(cut_ids)
    total = sum(c for sid, _f, _t, c in segments if sid in cut_id_set)
    return {
        "source_side": sorted(minimal_side),
        "cut_segments": cut_ids,
        "total_cost": total,
    }


def all_valid_zones(
    area_ids: Sequence[str],
) -> Iterable[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Yield every legal (sources, sinks) split of non-empty disjoint sets.

    Implemented as a three-state enumeration per area: neutral / source /
    sink, keeping only codes where both source and sink sets are
    non-empty.
    """
    areas = list(area_ids)
    n = len(areas)
    for value in range(3**n):
        sources = []
        sinks = []
        x = value
        for i in range(n):
            digit = x % 3
            x //= 3
            if digit == 1:
                sources.append(areas[i])
            elif digit == 2:
                sinks.append(areas[i])
        if sources and sinks:
            yield tuple(sources), tuple(sinks)
