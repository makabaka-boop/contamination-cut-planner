"""End-to-end HTTP tests against an in-memory repository."""

from __future__ import annotations

import copy
import json

import pytest

PLAN = {
    "areas": ["src", "a", "b", "sink"],
    "segments": [
        {"id": "e1", "from": "src", "to": "a", "cost": 3},
        {"id": "e2", "from": "a", "to": "b", "cost": 4},
        {"id": "e3", "from": "b", "to": "sink", "cost": 5},
        {"id": "e4", "from": "src", "to": "sink", "cost": 9},
    ],
    "sources": ["src"],
    "sinks": ["sink"],
}


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_save_get_compute_adopt_happy_path(client):
    r = client.put("/api/plans/zone-a", json=PLAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1
    assert len(body["content_hash"]) == 64

    r = client.get("/api/plans/zone-a")
    assert r.status_code == 200
    assert r.json()["plan"]["areas"] == ["a", "b", "sink", "src"]  # canonical sort

    r = client.post("/api/plans/zone-a/compute")
    assert r.status_code == 200, r.text
    computed = r.json()
    assert computed["plan_id"] == "zone-a"
    result = computed["result"]
    # Source side {src} crosses both edges leaving src: e1 (3) + e4 (9)
    # = 12, tying the sink-side alternative e3+e4=14... minimum is 12
    # versus closing the path edge e3 only (5) plus e4 (9) = 14.
    assert result["cut_segments"] == ["e1", "e4"]
    assert result["total_cost"] == 12
    assert result["source_side"] == ["src"]
    cid = computed["computation_id"]

    r = client.post(f"/api/results/{cid}/adopt")
    assert r.status_code == 200, r.text
    adopted = r.json()
    assert adopted["computation_id"] == cid
    assert adopted["snapshot"]["result"] == result
    assert adopted["snapshot"]["plan"]["segments"][0]["id"] == "e1"

    r = client.get("/api/adopted")
    assert r.status_code == 200
    assert r.json()["snapshot"]["plan_id"] == "zone-a"
    assert r.json()["computation_id"] == cid


def test_compute_missing_plan_404(client):
    r = client.post("/api/plans/nope/compute")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PLAN_NOT_FOUND"


def test_adopt_missing_result_404_and_leaves_state(client):
    r = client.put("/api/plans/p", json=PLAN)
    assert r.status_code == 200
    fake = "00000000-0000-4000-8000-000000000000"
    r = client.post(f"/api/results/{fake}/adopt")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESULT_NOT_FOUND"

    r = client.get("/api/adopted")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ADOPTED_RESULT_NOT_FOUND"


def test_adopt_malformed_id_404(client):
    r = client.post("/api/results/not-a-uuid/adopt")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESULT_NOT_FOUND"


def test_invalid_plan_does_not_overwrite_existing(client):
    r = client.put("/api/plans/p", json=PLAN)
    assert r.status_code == 200
    before = r.json()

    bad = copy.deepcopy(PLAN)
    bad["sources"] = ["ghost"]
    r = client.put("/api/plans/p", json=bad)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNKNOWN_AREA"

    r = client.get("/api/plans/p")
    assert r.status_code == 200
    after = r.json()
    assert after["version"] == 1
    assert after["content_hash"] == before["content_hash"]


def test_failed_computation_does_not_affect_adopted(client):
    # Valid save, compute and adopt.
    client.put("/api/plans/p", json=PLAN)
    cid = client.post("/api/plans/p/compute").json()["computation_id"]
    client.post(f"/api/results/{cid}/adopt")

    # Adopting a different nonexistent id must not replace the snapshot.
    r = client.post(
        "/api/results/11111111-1111-4111-8111-111111111111/adopt"
    )
    assert r.status_code == 404

    r = client.get("/api/adopted")
    assert r.json()["computation_id"] == cid


def test_plan_version_bumps_on_change_and_hash_stable_on_equivalent(client):
    reordered = copy.deepcopy(PLAN)
    reordered["segments"] = list(reversed(reordered["segments"]))
    reordered["areas"] = list(reversed(reordered["areas"]))

    r1 = client.put("/api/plans/p", json=PLAN)
    r2 = client.put("/api/plans/p", json=reordered)
    assert r1.json()["content_hash"] == r2.json()["content_hash"]
    assert r2.json()["version"] == 2

    changed = copy.deepcopy(PLAN)
    changed["segments"][0]["cost"] = 100
    r3 = client.put("/api/plans/p", json=changed)
    assert r3.json()["version"] == 3
    assert r3.json()["content_hash"] != r1.json()["content_hash"]


def test_recompute_same_content_returns_same_computation_id(client):
    client.put("/api/plans/p", json=PLAN)
    c1 = client.post("/api/plans/p/compute").json()["computation_id"]
    c2 = client.post("/api/plans/p/compute").json()["computation_id"]
    assert c1 == c2


def test_order_independent_results_end_to_end(client):
    a = copy.deepcopy(PLAN)
    b = copy.deepcopy(PLAN)
    b["segments"] = list(reversed(b["segments"]))
    client.put("/api/plans/a", json=a)
    client.put("/api/plans/b", json=b)
    ra = client.post("/api/plans/a/compute").json()["result"]
    rb = client.post("/api/plans/b/compute").json()["result"]
    assert ra == rb


def test_fresh_service_instance_returns_identical_result():
    """A restarted service (new repository + app) computes the same list."""
    from app.api import create_app
    from app.repository import InMemoryRepository
    from fastapi.testclient import TestClient

    def run(segments):
        app = create_app(repository=InMemoryRepository())
        with TestClient(app) as c:
            doc = copy.deepcopy(PLAN)
            doc["segments"] = segments
            c.put("/api/plans/p", json=doc)
            return c.post("/api/plans/p/compute").json()

    first = run(PLAN["segments"])
    second = run(list(reversed(PLAN["segments"])))
    assert first["result"] == second["result"]
    assert first["content_hash"] == second["content_hash"]
    # The UUID computation ids differ only because they are separate
    # stores; the deterministic payload is the result and hash.
    assert first["result"]["cut_segments"] == ["e1", "e4"]


def test_adopt_snapshot_is_complete(client):
    client.put("/api/plans/p", json=PLAN)
    cid = client.post("/api/plans/p/compute").json()["computation_id"]
    snap = client.post(f"/api/results/{cid}/adopt").json()["snapshot"]
    assert snap["computation_id"] == cid
    assert snap["plan_version"] == 1
    assert snap["content_hash"]
    assert snap["plan"] == client.get("/api/plans/p").json()["plan"]
    assert snap["result"]["total_cost"] == 12


def test_validation_error_codes_are_stable(client):
    cases = [
        ({}, "MISSING_FIELDS"),
        (
            {
                "areas": ["a", "b"],
                "segments": [],
                "sources": [],
                "sinks": ["b"],
                "x": 1,
            },
            "UNEXPECTED_FIELDS",
        ),
        ({"areas": ["a", "b"], "segments": [], "sources": [], "sinks": ["b"]}, "EMPTY_SOURCES"),
        ({"areas": ["a"], "segments": [], "sources": ["a"], "sinks": ["a"]}, "SOURCE_SINK_OVERLAP"),
        (
            {
                "areas": ["a", "b", "a"],
                "segments": [],
                "sources": ["a"],
                "sinks": ["b"],
            },
            "DUPLICATE_AREA",
        ),
        (
            {
                "areas": ["a", "b"],
                "segments": [{"id": "e", "from": "a", "to": "b", "cost": -1}],
                "sources": ["a"],
                "sinks": ["b"],
            },
            "INVALID_COST",
        ),
        (
            {
                "areas": ["a", "b"],
                "segments": [{"id": "e", "from": "a", "to": "z", "cost": 1}],
                "sources": ["a"],
                "sinks": ["b"],
            },
            "UNKNOWN_AREA",
        ),
        (
            {
                "areas": ["a", "b"],
                "segments": [{"id": "bad id!", "from": "a", "to": "b", "cost": 1}],
                "sources": ["a"],
                "sinks": ["b"],
            },
            "INVALID_ID",
        ),
        (
            {
                "areas": ["a", "b"],
                "segments": [
                    {"id": "e", "from": "a", "to": "b", "cost": 1},
                    {"id": "e", "from": "b", "to": "a", "cost": 1},
                ],
                "sources": ["a"],
                "sinks": ["b"],
            },
            "DUPLICATE_SEGMENT",
        ),
    ]
    for doc, code in cases:
        r = client.put("/api/plans/p", json=doc)
        assert r.status_code == 422, doc
        assert r.json()["error"]["code"] == code, (doc, r.json())


def test_malformed_json_and_non_object(client):
    r = client.put("/api/plans/p", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_JSON"

    r = client.put("/api/plans/p", content=b"[1,2]", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_JSON"

    r = client.put(
        "/api/plans/p",
        content=b'{"areas": [], "areas": [], "segments": [], "sources": [], "sinks": []}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DUPLICATE_JSON_KEY"


def test_action_endpoint_rejects_parameters(client):
    client.put("/api/plans/p", json=PLAN)
    r = client.post("/api/plans/p/compute", json={"force": True})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNEXPECTED_FIELDS"


def test_multi_source_multi_sink_and_64bit_total(client):
    # Edge costs are capped at 10^9; exceed 32 bits by accumulating
    # parallel max-cost edges across the multi-source/multi-sink cut:
    # 2 sources feed m (two 10^9 edges) and m leaves to 2 sinks through
    # five 10^9 edges, so the sink-side cut totals 5*10^9 > 2^32-1.
    big = 1_000_000_000
    segments = []
    segments += [
        {"id": f"a{i}", "from": "s1", "to": "m", "cost": big} for i in range(3)
    ]
    segments += [
        {"id": f"b{i}", "from": "s2", "to": "m", "cost": big} for i in range(3)
    ]
    segments += [
        {"id": f"c{i}", "from": "m", "to": ("t1" if i % 2 == 0 else "t2"), "cost": big}
        for i in range(5)
    ]
    doc = {
        "areas": ["s1", "s2", "t1", "t2", "m"],
        "segments": segments,
        "sources": ["s1", "s2"],
        "sinks": ["t1", "t2"],
    }
    client.put("/api/plans/big", json=doc)
    r = client.post("/api/plans/big/compute")
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["total_cost"] == 5 * big
    assert result["total_cost"] > (1 << 32) - 1
    assert isinstance(result["total_cost"], int)
    # snapshot stores the 64-bit value verbatim in JSON
    r2 = client.post(f"/api/results/{r.json()['computation_id']}/adopt")
    payload = json.loads(r2.content)
    assert payload["snapshot"]["result"]["total_cost"] == result["total_cost"]


def test_snapshot_freezes_plan_even_after_later_edits(client):
    client.put("/api/plans/p", json=PLAN)
    cid = client.post("/api/plans/p/compute").json()["computation_id"]

    edited = copy.deepcopy(PLAN)
    edited["segments"][0] = {"id": "e1", "from": "src", "to": "a", "cost": 999}
    client.put("/api/plans/p", json=edited)

    snap = client.post(f"/api/results/{cid}/adopt").json()["snapshot"]
    # The adopted snapshot keeps the version-1 cost, not the new one.
    e1 = next(s for s in snap["plan"]["segments"] if s["id"] == "e1")
    assert e1["cost"] == 3
    assert snap["plan_version"] == 1
