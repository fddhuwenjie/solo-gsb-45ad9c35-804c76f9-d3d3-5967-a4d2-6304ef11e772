"""API integration tests for safe decommissioning with an atomic plan swap."""

from __future__ import annotations

import sqlite3
import threading

from fastapi.testclient import TestClient

RELS = [
    {"type": "customer_provider", "customer": 10, "provider": 100},
    {"type": "peer", "a": 100, "b": 50},
    {"type": "customer_provider", "customer": 60, "provider": 50},
]
# Announcements covering the two halves of 203.0.113.0/24.
ANN_BOTH = [
    {"id": "a0", "prefix": "203.0.113.0/25", "as_path": [10, 100]},
    {"id": "a128", "prefix": "203.0.113.128/25", "as_path": [100, 50, 60]},
]
# Successor announcements covering only the lower half (post-release state).
ANN_LOW = [{"id": "a0", "prefix": "203.0.113.0/25", "as_path": [10, 100]}]


def _create_pool(client, cidr="203.0.113.0/24", exclusions=None):
    resp = client.post(
        "/pools", json={"cidr": cidr, "exclusions": exclusions or []}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _allocate(client, pool_id, request_id, prefix_length):
    resp = client.post(
        f"/pools/{pool_id}/allocations",
        json={"request_id": request_id, "prefix_length": prefix_length},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["cidr"]


def _create_plan(client, pool_id, plan_id, announcements=None, relationships=None):
    resp = client.post(
        f"/pools/{pool_id}/bgp-plans",
        json={
            "plan_id": plan_id,
            "relationships": RELS if relationships is None else relationships,
            "announcements": ANN_BOTH if announcements is None else announcements,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _activate(client, pool_id, plan_id):
    resp = client.post(f"/pools/{pool_id}/bgp-plans/{plan_id}/activate")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _body(
    request_id="order-2",
    active_plan_id="plan-1",
    plan_id="plan-2",
    relationships=None,
    announcements=None,
):
    return {
        "request_id": request_id,
        "active_plan_id": active_plan_id,
        "plan_id": plan_id,
        "relationships": RELS if relationships is None else relationships,
        "announcements": ANN_LOW if announcements is None else announcements,
    }


def _decommission(client, pool_id, body=None):
    return client.post(
        f"/pools/{pool_id}/decommissions", json=body if body is not None else _body()
    )


def _plan_status(client, pool_id, plan_id, announcements=None):
    """Read a plan back through the idempotent create replay."""

    resp = client.post(
        f"/pools/{pool_id}/bgp-plans",
        json={
            "plan_id": plan_id,
            "relationships": RELS,
            "announcements": ANN_BOTH if announcements is None else announcements,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["replayed"] is True
    return resp.json()["status"]


def _seed_pool_with_active_plan(client, cidr="203.0.113.0/24", exclusions=None):
    """Pool with two /25 allocations and an active plan covering both."""

    pid = _create_pool(client, cidr=cidr, exclusions=exclusions)
    _allocate(client, pid, "order-1", 25)  # 203.0.113.0/25
    _allocate(client, pid, "order-2", 25)  # 203.0.113.128/25
    _create_plan(client, pid, "plan-1")
    _activate(client, pid, "plan-1")
    return pid


def test_decommission_releases_and_swaps_plan_atomically(client):
    pid = _seed_pool_with_active_plan(client)

    resp = _decommission(client, pid)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pool_id"] == pid
    assert body["released"] == "203.0.113.128/25"
    assert body["request_id"] == "order-2"

    # Response order: the retired outgoing plan, then the active successor.
    assert [p["plan_id"] for p in body["plans"]] == ["plan-1", "plan-2"]
    retired, successor = body["plans"]
    assert retired["status"] == "retired"
    assert retired["activated_at"] is not None
    assert successor["status"] == "active"
    assert successor["activated_at"] is not None
    assert successor["replayed"] is False

    # The successor is pinned to the post-release pool state: only the
    # remaining allocation is listed.
    pinned = successor["pool_state"]
    assert pinned["cidr"] == "203.0.113.0/24"
    assert [a["cidr"] for a in pinned["allocations"]] == ["203.0.113.0/25"]
    assert [r["id"] for r in successor["results"]] == ["a0"]

    # The release is a real release: the pool shows one allocation and the
    # freed /25 is back in the free list.
    status = client.get(f"/pools/{pid}").json()
    assert [a["request_id"] for a in status["allocated"]] == ["order-1"]
    assert status["free_cidrs"] == ["203.0.113.128/25"]

    # Stored statuses are visible through the idempotent create replay.
    assert _plan_status(client, pid, "plan-1") == "retired"
    assert _plan_status(client, pid, "plan-2", announcements=ANN_LOW) == "active"


def test_decommission_successor_digest_matches_fresh_plan(client):
    # A standalone plan created right after the decommission pins the same
    # pool state, so both digests must agree.
    pid = _seed_pool_with_active_plan(client)
    resp = _decommission(client, pid)
    assert resp.status_code == 200, resp.text
    successor = resp.json()["plans"][1]

    created = _create_plan(client, pid, "plan-3", announcements=ANN_LOW)
    assert created["state_digest"] == successor["state_digest"]
    assert created["pool_state"] == successor["pool_state"]


def test_decommissioned_pool_keeps_working_with_existing_endpoints(client):
    pid = _seed_pool_with_active_plan(client)
    assert _decommission(client, pid).status_code == 200

    # The freed space can be allocated again through the ordinary endpoint.
    cidr = _allocate(client, pid, "order-3", 25)
    assert cidr == "203.0.113.128/25"

    # The decommission-stored plan is an ordinary active plan: a standalone
    # successor can be created and handed over from it.
    _create_plan(client, pid, "plan-3")
    handover = client.post(
        f"/pools/{pid}/bgp-plans/handover",
        json={"old_plan_ids": ["plan-2"], "new_plan_id": "plan-3"},
    )
    assert handover.status_code == 200, handover.text
    statuses = {p["plan_id"]: p["status"] for p in handover.json()["plans"]}
    assert statuses == {"plan-2": "retired", "plan-3": "active"}

    # The ordinary release endpoint still works.
    resp = client.delete(f"/pools/{pid}/allocations/order-3")
    assert resp.status_code == 200
    assert resp.json()["released"] == "203.0.113.128/25"


def test_decommission_is_not_replayable(client):
    pid = _seed_pool_with_active_plan(client)
    assert _decommission(client, pid).status_code == 200

    # Repeating the same request: the outgoing plan is retired and the
    # successor id is taken, so the request conflicts and nothing changes.
    resp = _decommission(client, pid)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_decommission_active_mismatch"
    status = client.get(f"/pools/{pid}").json()
    assert [a["request_id"] for a in status["allocated"]] == ["order-1"]
    assert _plan_status(client, pid, "plan-1") == "retired"
    assert _plan_status(client, pid, "plan-2", announcements=ANN_LOW) == "active"


def test_decommission_requires_exactly_the_named_active_plan(client):
    pid = _seed_pool_with_active_plan(client)

    # The named plan is not the active one (plan-x is only planned).
    _create_plan(client, pid, "plan-x")
    resp = _decommission(client, pid, _body(active_plan_id="plan-x", plan_id="plan-2"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_decommission_active_mismatch"

    # A stored planned plan cannot be used as the successor id either.
    resp = _decommission(client, pid, _body(active_plan_id="plan-1", plan_id="plan-x"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_id_conflict"

    # Nothing changed: both allocations and the active plan are intact.
    status = client.get(f"/pools/{pid}").json()
    assert len(status["allocated"]) == 2
    assert _plan_status(client, pid, "plan-1") == "active"


def test_decommission_rejects_pool_without_active_plan(client):
    pid = _create_pool(client)
    _allocate(client, pid, "order-1", 25)
    _allocate(client, pid, "order-2", 25)
    _create_plan(client, pid, "plan-1")  # planned, never activated

    resp = _decommission(client, pid)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_decommission_active_mismatch"
    assert len(client.get(f"/pools/{pid}").json()["allocated"]) == 2


def test_decommission_rejects_multiple_active_plans(client):
    # Legacy databases may hold several active plans in one pool; the
    # decommission refuses to guess and changes nothing.
    pid = _seed_pool_with_active_plan(client)
    from app import db as db_module

    conn = sqlite3.connect(db_module.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO bgp_plans (plan_id, pool_id, params, audit_results, "
            "pool_state, state_digest, status, created_at, activated_at) "
            "VALUES ('legacy-extra', ?, '{}', '[]', '{}', 'seed', 'active', "
            "'2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00')",
            (pid,),
        )
        conn.commit()
    finally:
        conn.close()

    resp = _decommission(client, pid)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_decommission_active_mismatch"
    assert len(client.get(f"/pools/{pid}").json()["allocated"]) == 2
    assert _plan_status(client, pid, "plan-1") == "active"


def test_decommission_rejects_stale_outgoing_plan_state(client):
    # A /23 pool leaves room for the pool to drift after the plan is pinned.
    pid = _create_pool(client, cidr="203.0.112.0/23")
    _allocate(client, pid, "order-1", 25)  # 203.0.112.0/25
    _allocate(client, pid, "order-2", 25)  # 203.0.112.128/25
    pair_anns = [
        {"id": "a0", "prefix": "203.0.112.0/25", "as_path": [10, 100]},
        {"id": "a128", "prefix": "203.0.112.128/25", "as_path": [10, 100]},
    ]
    _create_plan(client, pid, "plan-1", announcements=pair_anns)
    _activate(client, pid, "plan-1")
    # The pool drifts after plan-1 was pinned: a new allocation appears.
    _allocate(client, pid, "order-3", 26)  # 203.0.113.0/26

    resp = _decommission(
        client,
        pid,
        _body(announcements=[pair_anns[0]]),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_state_conflict"

    # Nothing changed: all three allocations remain, plan-1 still active,
    # no successor stored (plan-2 is still a free id, so a fresh standalone
    # plan can take it).
    status = client.get(f"/pools/{pid}").json()
    assert len(status["allocated"]) == 3
    assert _plan_status(client, pid, "plan-1", announcements=pair_anns) == "active"
    probe_anns = [
        *pair_anns,
        {"id": "a113", "prefix": "203.0.113.0/26", "as_path": [10, 100]},
    ]
    plans = client.post(
        f"/pools/{pid}/bgp-plans",
        json={"plan_id": "plan-2", "relationships": RELS, "announcements": probe_anns},
    )
    assert plans.status_code == 201  # plan-2 was never stored; it was free


def test_decommission_unknown_allocation_is_404(client):
    pid = _seed_pool_with_active_plan(client)
    resp = _decommission(client, pid, _body(request_id="nope"))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "allocation_not_found"
    assert len(client.get(f"/pools/{pid}").json()["allocated"]) == 2
    assert _plan_status(client, pid, "plan-1") == "active"


def test_decommission_unknown_or_foreign_plan_is_404(client):
    pid = _seed_pool_with_active_plan(client)
    other = _create_pool(client, cidr="203.0.114.0/24")
    _allocate(client, other, "order-9", 25)
    _create_plan(
        client,
        other,
        "plan-foreign",
        announcements=[{"id": "f", "prefix": "203.0.114.0/25", "as_path": [10, 100]}],
    )
    _activate(client, other, "plan-foreign")

    resp = _decommission(client, pid, _body(active_plan_id="nope"))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "bgp_plan_not_found"

    resp = _decommission(client, pid, _body(active_plan_id="plan-foreign"))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "bgp_plan_not_found"

    resp = _decommission(client, 999)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "pool_not_found"


def test_decommission_successor_plan_id_taken_conflicts(client):
    pid = _seed_pool_with_active_plan(client)
    _create_plan(client, pid, "plan-2")  # standalone plan already stores the id

    resp = _decommission(client, pid)  # successor plan_id already stored
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_id_conflict"

    # The allocation was not released and plan-1 is still active.
    assert len(client.get(f"/pools/{pid}").json()["allocated"]) == 2
    assert _plan_status(client, pid, "plan-1") == "active"
    assert _plan_status(client, pid, "plan-2") == "planned"


def test_decommission_successor_id_equal_to_outgoing_id_conflicts(client):
    pid = _seed_pool_with_active_plan(client)
    resp = _decommission(client, pid, _body(plan_id="plan-1"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_id_conflict"
    assert len(client.get(f"/pools/{pid}").json()["allocated"]) == 2
    assert _plan_status(client, pid, "plan-1") == "active"


def test_decommission_announcement_must_not_intersect_released_cidr(client):
    pid = _seed_pool_with_active_plan(client)
    # The successor set still announces the block being withdrawn.
    resp = _decommission(
        client,
        pid,
        _body(
            announcements=[
                {"id": "a0", "prefix": "203.0.113.0/25", "as_path": [10, 100]},
                {"id": "gone", "prefix": "203.0.113.128/25", "as_path": [10, 100]},
            ]
        ),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_announcement_overlaps_released"

    # A less specific announcement covering the released block intersects too.
    resp = _decommission(
        client,
        pid,
        _body(
            announcements=[
                {"id": "all", "prefix": "203.0.113.0/24", "as_path": [10, 100]},
            ]
        ),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_announcement_overlaps_released"

    # Nothing changed either time.
    assert len(client.get(f"/pools/{pid}").json()["allocated"]) == 2
    assert _plan_status(client, pid, "plan-1") == "active"


def test_decommission_uncovered_remaining_allocation_rolls_back(client):
    pid = _seed_pool_with_active_plan(client)
    # Announce only a quarter of the remaining /25.
    resp = _decommission(
        client,
        pid,
        _body(
            announcements=[
                {"id": "half", "prefix": "203.0.113.0/26", "as_path": [10, 100]},
            ]
        ),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_allocation_uncovered"
    assert len(client.get(f"/pools/{pid}").json()["allocated"]) == 2
    assert _plan_status(client, pid, "plan-1") == "active"


def test_decommission_origin_conflict_on_remaining_allocation_rolls_back(client):
    pid = _seed_pool_with_active_plan(client)
    resp = _decommission(
        client,
        pid,
        _body(
            announcements=[
                {"id": "a0", "prefix": "203.0.113.0/25", "as_path": [10, 100]},
                {"id": "a0b", "prefix": "203.0.113.0/25", "as_path": [100, 50, 60]},
            ]
        ),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_allocation_origin_conflict"
    assert len(client.get(f"/pools/{pid}").json()["allocated"]) == 2
    assert _plan_status(client, pid, "plan-1") == "active"


def test_decommission_announcement_outside_pool_or_exclusion_rolls_back(client):
    # Pool with an exclusion; two /26 allocations stay clear of it.
    pid = _create_pool(client, exclusions=["203.0.113.192/26"])
    _allocate(client, pid, "order-1", 26)  # 203.0.113.0/26
    _allocate(client, pid, "order-2", 26)  # 203.0.113.64/26
    pair_anns = [
        {"id": "a0", "prefix": "203.0.113.0/26", "as_path": [10, 100]},
        {"id": "a64", "prefix": "203.0.113.64/26", "as_path": [10, 100]},
    ]
    _create_plan(client, pid, "plan-1", announcements=pair_anns)
    _activate(client, pid, "plan-1")
    low_anns = [{"id": "a0", "prefix": "203.0.113.0/26", "as_path": [10, 100]}]

    resp = _decommission(
        client,
        pid,
        _body(
            announcements=[
                {"id": "out", "prefix": "203.0.114.0/26", "as_path": [10, 100]},
                *low_anns,
            ]
        ),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_announcement_outside_pool"

    resp = _decommission(
        client,
        pid,
        _body(
            announcements=[
                *low_anns,
                {"id": "bad", "prefix": "203.0.113.192/26", "as_path": [10, 100]},
            ]
        ),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_announcement_overlaps_exclusion"

    assert len(client.get(f"/pools/{pid}").json()["allocated"]) == 2
    assert _plan_status(client, pid, "plan-1", announcements=pair_anns) == "active"


def test_decommission_audit_failure_leaves_everything_untouched(client):
    pid = _seed_pool_with_active_plan(client)
    # Unknown relationship edge in the successor path -> request-level 422.
    resp = _decommission(
        client,
        pid,
        _body(
            announcements=[
                {"id": "a0", "prefix": "203.0.113.0/25", "as_path": [10, 200]},
            ]
        ),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "as_relation_unknown"

    # Duplicate announcement ids -> 422.
    resp = _decommission(
        client,
        pid,
        _body(
            announcements=[
                {"id": "a0", "prefix": "203.0.113.0/25", "as_path": [10, 100]},
                {"id": "a0", "prefix": "203.0.113.0/26", "as_path": [10, 100]},
            ]
        ),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "duplicate_announcement_id"

    assert len(client.get(f"/pools/{pid}").json()["allocated"]) == 2
    assert _plan_status(client, pid, "plan-1") == "active"


def test_decommission_request_shape_validation(client):
    pid = _seed_pool_with_active_plan(client)
    base = _body()

    for key in ("request_id", "active_plan_id", "plan_id"):
        bad = dict(base, **{key: ""})
        assert _decommission(client, pid, bad).status_code == 422

    bad = dict(base, announcements=[])
    assert _decommission(client, pid, bad).status_code == 422

    bad = dict(base)
    del bad["announcements"]
    assert _decommission(client, pid, bad).status_code == 422

    # Nothing was applied by the rejected requests.
    assert len(client.get(f"/pools/{pid}").json()["allocated"]) == 2
    assert _plan_status(client, pid, "plan-1") == "active"


def test_decommission_keeps_provision_history_replayable(client):
    # Provision one subnet with its plan, activate the plan, add a plain
    # allocation, then decommission the provisioned allocation.
    pid = _create_pool(client)
    provision_body = {
        "request_id": "order-1",
        "prefix_length": 25,
        "plan_id": "plan-1",
        "relationships": RELS,
        "announcements": [],
        "new_announcement": {"id": "new", "as_path": [10, 100]},
    }
    resp = client.post(f"/pools/{pid}/provisions", json=provision_body)
    assert resp.status_code == 201, resp.text
    original = resp.json()
    assert client.post(f"/pools/{pid}/bgp-plans/plan-1/activate").status_code == 200
    _allocate(client, pid, "order-2", 25)  # 203.0.113.128/25

    # The active plan's pinned state is stale now (order-2 was added), so
    # refresh it through a handover before decommissioning.
    _create_plan(client, pid, "plan-1b")
    handover = client.post(
        f"/pools/{pid}/bgp-plans/handover",
        json={"old_plan_ids": ["plan-1"], "new_plan_id": "plan-1b"},
    )
    assert handover.status_code == 200, handover.text

    resp = _decommission(
        client,
        pid,
        _body(
            request_id="order-1",
            active_plan_id="plan-1b",
            plan_id="plan-2",
            announcements=[
                {"id": "a128", "prefix": "203.0.113.128/25", "as_path": [10, 100]}
            ],
        ),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["released"] == "203.0.113.0/25"

    # The provision record survived: the same provision request replays the
    # historical allocation and plan without re-occupying the freed address.
    replay = client.post(f"/pools/{pid}/provisions", json=provision_body)
    assert replay.status_code == 200, replay.text
    data = replay.json()
    assert data["replayed"] is True
    assert data["allocation"]["id"] == original["allocation"]["id"]
    assert data["allocation"]["cidr"] == "203.0.113.0/25"
    assert data["plan"]["plan_id"] == "plan-1"
    assert data["plan"]["status"] == "retired"

    status = client.get(f"/pools/{pid}").json()
    assert [a["request_id"] for a in status["allocated"]] == ["order-2"]

    # The freed request_id can be reused by an ordinary allocation while the
    # historical provision replay is unaffected.
    ordinary = client.post(
        f"/pools/{pid}/allocations",
        json={"request_id": "order-1", "prefix_length": 26},
    )
    assert ordinary.status_code == 201, ordinary.text
    assert ordinary.json()["cidr"] == "203.0.113.0/26"
    replay = client.post(f"/pools/{pid}/provisions", json=provision_body)
    assert replay.status_code == 200
    assert replay.json()["allocation"]["cidr"] == "203.0.113.0/25"


def test_decommissioned_request_id_and_plan_keys_behave_like_releases(client):
    pid = _seed_pool_with_active_plan(client)
    assert _decommission(client, pid).status_code == 200

    # The released request_id is free for a genuinely new provision.
    provision_body = {
        "request_id": "order-2",
        "prefix_length": 25,
        "plan_id": "plan-9",
        "relationships": RELS,
        "announcements": [
            {"id": "cur", "prefix": "203.0.113.0/25", "as_path": [10, 100]}
        ],
        "new_announcement": {"id": "new", "as_path": [10, 100]},
    }
    resp = client.post(f"/pools/{pid}/provisions", json=provision_body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["allocation"]["cidr"] == "203.0.113.128/25"


def test_concurrent_decommissions_only_one_commits(client):
    pid = _seed_pool_with_active_plan(client)

    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, dict]] = []
    lock = threading.Lock()

    from app.main import app

    def worker(plan_id):
        local = TestClient(app)
        barrier.wait()
        resp = local.post(
            f"/pools/{pid}/decommissions",
            json=_body(plan_id=plan_id),
        )
        with lock:
            outcomes.append((resp.status_code, resp.json()))

    threads = [
        threading.Thread(target=worker, args=("plan-2",)),
        threading.Thread(target=worker, args=("plan-3",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(code for code, _ in outcomes)
    assert statuses == [200, 409]
    # Exactly one decommission happened: one allocation released, plan-1
    # retired, exactly one successor active.
    status = client.get(f"/pools/{pid}").json()
    assert [a["request_id"] for a in status["allocated"]] == ["order-1"]
    assert _plan_status(client, pid, "plan-1") == "retired"
    winner = "plan-2" if _try_plan_status(client, pid, "plan-2") else "plan-3"
    assert _plan_status(client, pid, winner, announcements=ANN_LOW) == "active"


def _try_plan_status(client, pool_id, plan_id):
    """True when a plan row exists (probed via the create replay/conflict)."""

    resp = client.post(
        f"/pools/{pool_id}/bgp-plans",
        json={"plan_id": plan_id, "relationships": RELS, "announcements": ANN_LOW},
    )
    return resp.status_code == 200
