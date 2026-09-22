"""Strict validation and canonical normalization of plan documents."""

from __future__ import annotations

import re
from typing import Any

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_AREAS = 300
MAX_SEGMENTS = 2000
COST_MAX = 1_000_000_000


class ValidationError(Exception):
    """Raised with a stable machine-readable code for invalid documents."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def validate_plan(doc: Any) -> dict:
    """Validate a plan JSON document and return a canonical copy.

    Rules (failures all use stable error codes, never HTTP 500):

    * exactly the four keys ``areas`` / ``segments`` / ``sources`` /
      ``sinks`` must be present at the top level;
    * every area/segment id matches ``[A-Za-z0-9_-]{1,32}`` and is unique
      within its own id space;
    * each segment has exactly ``id`` / ``from`` / ``to`` / ``cost``;
      cost is an integer in ``[0, 10**9]``; endpoints reference existing
      areas; segments are directed (no implicit reversal);
    * sources and sinks are non-empty, disjoint lists of existing areas;
    * at most 300 areas and 2000 segments.

    The returned document has all lists sorted so that two submissions
    differing only in item order normalize to the same bytes -- this is
    what guarantees order-independent computation ids and snapshots.
    """
    if not isinstance(doc, dict):
        raise ValidationError("INVALID_PLAN", "request body must be a JSON object")

    required = {"areas", "segments", "sources", "sinks"}
    present = set(doc.keys())
    missing = required - present
    if missing:
        raise ValidationError(
            "MISSING_FIELDS", f"missing required fields: {sorted(missing)}"
        )
    extra = present - required
    if extra:
        raise ValidationError(
            "UNEXPECTED_FIELDS", f"unexpected fields: {sorted(extra)}"
        )

    raw_areas = doc["areas"]
    raw_segments = doc["segments"]
    raw_sources = doc["sources"]
    raw_sinks = doc["sinks"]

    if not isinstance(raw_areas, list):
        raise ValidationError("INVALID_AREAS", "'areas' must be a list")
    if not isinstance(raw_segments, list):
        raise ValidationError("INVALID_SEGMENTS", "'segments' must be a list")
    if not isinstance(raw_sources, list):
        raise ValidationError("INVALID_SOURCES", "'sources' must be a list")
    if not isinstance(raw_sinks, list):
        raise ValidationError("INVALID_SINKS", "'sinks' must be a list")

    if len(raw_areas) > MAX_AREAS:
        raise ValidationError("TOO_MANY_AREAS", f"at most {MAX_AREAS} areas allowed")
    if len(raw_segments) > MAX_SEGMENTS:
        raise ValidationError(
            "TOO_MANY_SEGMENTS", f"at most {MAX_SEGMENTS} segments allowed"
        )

    # ---- areas -----------------------------------------------------------
    areas = []
    area_set: set[str] = set()
    for pos, item in enumerate(raw_areas):
        if not _is_str(item):
            raise ValidationError(
                "INVALID_AREA", f"areas[{pos}] must be an id string"
            )
        if not ID_RE.fullmatch(item):
            raise ValidationError(
                "INVALID_ID", f"areas[{pos}]={item!r} does not match [A-Za-z0-9_-]{{1,32}}"
            )
        if item in area_set:
            raise ValidationError("DUPLICATE_AREA", f"duplicate area id: {item}")
        area_set.add(item)
        areas.append(item)

    # ---- segments --------------------------------------------------------
    segments = []
    segment_set: set[str] = set()
    for pos, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            raise ValidationError(
                "INVALID_SEGMENT", f"segments[{pos}] must be an object"
            )
        keys = set(item.keys())
        expected = {"id", "from", "to", "cost"}
        if keys != expected:
            missing_seg = sorted(expected - keys)
            extra_seg = sorted(keys - expected)
            detail = []
            if missing_seg:
                detail.append(f"missing {missing_seg}")
            if extra_seg:
                detail.append(f"unexpected {extra_seg}")
            raise ValidationError(
                "INVALID_SEGMENT", f"segments[{pos}] fields invalid: {'; '.join(detail)}"
            )
        seg_id = item["id"]
        frm = item["from"]
        to = item["to"]
        cost = item["cost"]
        if not _is_str(seg_id) or not ID_RE.fullmatch(seg_id):
            raise ValidationError(
                "INVALID_ID", f"segments[{pos}].id does not match [A-Za-z0-9_-]{{1,32}}"
            )
        if seg_id in segment_set:
            raise ValidationError(
                "DUPLICATE_SEGMENT", f"duplicate segment id: {seg_id}"
            )
        segment_set.add(seg_id)
        if not _is_str(frm) or not ID_RE.fullmatch(frm):
            raise ValidationError(
                "INVALID_ID", f"segments[{pos}].from must be a valid id"
            )
        if not _is_str(to) or not ID_RE.fullmatch(to):
            raise ValidationError(
                "INVALID_ID", f"segments[{pos}].to must be a valid id"
            )
        if frm not in area_set:
            raise ValidationError(
                "UNKNOWN_AREA", f"segments[{pos}].from references unknown area {frm!r}"
            )
        if to not in area_set:
            raise ValidationError(
                "UNKNOWN_AREA", f"segments[{pos}].to references unknown area {to!r}"
            )
        if not _is_plain_int(cost):
            raise ValidationError(
                "INVALID_COST", f"segments[{pos}].cost must be an integer"
            )
        if not 0 <= cost <= COST_MAX:
            raise ValidationError(
                "INVALID_COST",
                f"segments[{pos}].cost must be within [0, 10^9]",
            )
        segments.append({"id": seg_id, "from": frm, "to": to, "cost": cost})

    # ---- sources / sinks -------------------------------------------------
    def _validate_zone(values: Any, field: str, code: str) -> list[str]:
        result = []
        seen: set[str] = set()
        if not values:
            raise ValidationError(code, f"'{field}' must be non-empty")
        for pos, value in enumerate(values):
            if not _is_str(value) or not ID_RE.fullmatch(value):
                raise ValidationError(
                    "INVALID_ID", f"{field}[{pos}] must be a valid area id"
                )
            if value not in area_set:
                raise ValidationError(
                    "UNKNOWN_AREA", f"{field}[{pos}] references unknown area {value!r}"
                )
            if value in seen:
                raise ValidationError(
                    "DUPLICATE_ZONE_AREA", f"{field} lists area {value!r} more than once"
                )
            seen.add(value)
            result.append(value)
        return result

    sources = _validate_zone(raw_sources, "sources", "EMPTY_SOURCES")
    sinks = _validate_zone(raw_sinks, "sinks", "EMPTY_SINKS")

    overlap = set(sources) & set(sinks)
    if overlap:
        raise ValidationError(
            "SOURCE_SINK_OVERLAP",
            f"areas cannot be both source and sink: {sorted(overlap)}",
        )

    # Canonicalize: sorting makes equal semantic content byte-equal, so
    # segment submission order never changes the stored plan or result.
    areas.sort()
    sources.sort()
    sinks.sort()
    segments.sort(key=lambda s: s["id"])

    return {
        "areas": areas,
        "segments": segments,
        "sources": sources,
        "sinks": sinks,
    }
