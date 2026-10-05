"""API integration tests for safe decommissioning with a BGP plan cutover."""

from __future__ import annotations

import sqlite3
import threading

from fastapi.testclient import TestClient

RELS = [{"type": "customer_provider", "customer": 10, "provider": 100}]
RELS_PEER = [
    {"type": "customer_provider", "customer": 10, "provider": 100},
    {"type": "peer", "a": 100, "b": 50},
    {"type": "customer_provider", "customer": 60, "provider": 50},
]


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


def _create_plan(client, pool_id, plan_id, announcements, relationships=None):
    resp = client.post(
        f"/pools/{pool_id}/bgp-plans",
        json={
            "plan_id": plan_id,
            "relationships": RELS if relationships is None else relationships,
            "announcements": announcements,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _activate(client, pool_id, plan_id):
    resp = client.post(f"/pools/{pool_id}/bgp-plans/{plan_id}/activate")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _seed_two_allocations_with_active_plan(client, plan_id="plan-a"):
    """Pool with two /25s covered by one /24 announcement and an active plan."""

    pid = _create_pool(client)
    cidr_0 = _allocate(client, pid, "order-1", 25)
    cidr_1 = _allocate(client, pid, "order-2", 25)
    assert (cidr_0, cidr_1) == ("203.0.113.0/25", "203.0.113.128/25")
    _create_plan(
        client,
        pid,
        plan_id,
        [{"id": "all", "prefix": "203.0.113.0/24", "as_path": [10, 100]}],
    )
    _activate(client, pid, plan_id)
    return pid


def _decommission(
    client,
    pool_id,
    request_id="order-2",
    active_plan_id="plan-a",
    new_plan_id="plan-b",
    announcements=None,
    relationships=None,
):
    if announcements is None:
        announcements = [
            {"id": "keep", "prefix": "203.0.113.0/25", "as_path": [10, 100]}
        ]
    return client.post(
        f"/pools/{pool_id}/decommission",
        json={
            "request_id": request_id,
            "active_plan_id": active_plan_id,
            "new_plan_id": new_plan_id,
            "relationships": RELS if relationships is None else relationships,
            "announcements": announcements,
        },
    )


def _plan_statuses(pool_id):
    from app import db as db_module

    conn = sqlite3.connect(db_module.DB_PATH)
    try:
        return dict(
            conn.execute(
                "SELECT plan_id, status FROM bgp_plans WHERE pool_id = ?",
                (pool_id,),
            ).fetchall()
        )
    finally:
        conn.close()


def test_decommission_releases_allocation_retires_and_activates(client):
    pid = _seed_two_allocations_with_active_plan(client)

    resp = _decommission(client, pid)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pool_id"] == pid
    assert body["released"] == "203.0.113.128/25"
    assert body["request_id"] == "order-2"
    # Old plan first, successor second.
    assert [p["plan_id"] for p in body["plans"]] == ["plan-a", "plan-b"]
    old_plan, new_plan = body["plans"]
    assert old_plan["status"] == "retired"
    assert old_plan["activated_at"] is not None
    assert new_plan["status"] == "active"
    assert new_plan["activated_at"] == new_plan["created_at"]

    # The successor is pinned to the post-release pool state.
    assert new_plan["pool_state"]["cidr"] == "203.0.113.0/24"
    assert [a["cidr"] for a in new_plan["pool_state"]["allocations"]] == [
        "203.0.113.0/25"
    ]
    assert new_plan["state_digest"] != old_plan["state_digest"]

    # The released allocation is gone; the remaining one stays.
    status = client.get(f"/pools/{pid}").json()
    assert [a["cidr"] for a in status["allocated"]] == ["203.0.113.0/25"]
    assert "203.0.113.128/25" in status["free_cidrs"]

    assert _plan_statuses(pid) == {"plan-a": "retired", "plan-b": "active"}


def test_successor_state_digest_matches_live_pool_snapshot(client):
    from app.bgpplan import _state_digest

    pid = _seed_two_allocations_with_active_plan(client)
    resp = _decommission(client, pid)
    assert resp.status_code == 200, resp.text
    successor = resp.json()["plans"][1]

    snapshot = client.get(f"/pools/{pid}/snapshot").json()["snapshot"]
    live_state = {
        "cidr": snapshot["pool"]["cidr"],
        "exclusions": snapshot["exclusions"],
        "allocations": [
            {
                "cidr": row["cidr"],
                "request_id": row["request_id"],
                "prefix_length": row["prefix_length"],
                "created_at": row["created_at"],
            }
            for row in snapshot["allocations"]
        ],
    }
    assert _state_digest(live_state) == successor["state_digest"]


def test_announcement_intersecting_released_cidr_is_rejected(client):
    pid = _seed_two_allocations_with_active_plan(client)

    # The /24 still announces the released .128/25.
    resp = _decommission(
        client,
        pid,
        announcements=[
            {"id": "all", "prefix": "203.0.113.0/24", "as_path": [10, 100]}
        ],
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_announcement_overlaps_released"

    # A more-specific inside the released range is rejected as well.
    resp = _decommission(
        client,
        pid,
        announcements=[
            {"id": "keep", "prefix": "203.0.113.0/25", "as_path": [10, 100]},
            {"id": "leak", "prefix": "203.0.113.128/26", "as_path": [10, 100]},
        ],
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_announcement_overlaps_released"

    # Nothing changed: both allocations present, plan still active.
    status = client.get(f"/pools/{pid}").json()
    assert {a["cidr"] for a in status["allocated"]} == {
        "203.0.113.0/25",
        "203.0.113.128/25",
    }
    assert _plan_statuses(pid) == {"plan-a": "active"}


def test_successor_must_cover_every_remaining_allocation(client):
    pid = _seed_two_allocations_with_active_plan(client)

    # Release order-1 (.0/25): the successor announcement sits inside the
    # remaining .128/25 but is only a /26, so it does not cover the whole
    # remaining allocation, and it never touches the released range.
    resp = _decommission(
        client,
        pid,
        request_id="order-1",
        announcements=[
            {"id": "keep", "prefix": "203.0.113.128/26", "as_path": [10, 100]}
        ],
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_allocation_uncovered"
    assert _plan_statuses(pid) == {"plan-a": "active"}
    # The target allocation was not released either.
    cidrs = {a["cidr"] for a in client.get(f"/pools/{pid}").json()["allocated"]}
    assert cidrs == {"203.0.113.0/25", "203.0.113.128/25"}


def test_successor_single_origin_per_remaining_allocation(client):
    pid = _seed_two_allocations_with_active_plan(client)

    # .0/25 covered by two valid announcements with different origins
    # (100 and 50).
    resp = _decommission(
        client,
        pid,
        announcements=[
            {"id": "o100", "prefix": "203.0.113.0/25", "as_path": [10, 100]},
            {"id": "o50", "prefix": "203.0.113.0/25", "as_path": [100, 50, 60]},
        ],
        relationships=RELS_PEER,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_allocation_origin_conflict"
    assert _plan_statuses(pid) == {"plan-a": "active"}


def test_successor_prefix_outside_pool_or_over_exclusion(client):
    pid = _seed_two_allocations_with_active_plan(client)
    resp = _decommission(
        client,
        pid,
        announcements=[
            {"id": "bad", "prefix": "203.0.112.0/25", "as_path": [10, 100]}
        ],
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_announcement_outside_pool"

    pid2 = _create_pool(client, cidr="198.51.100.0/24", exclusions=["198.51.100.0/26"])
    _allocate(client, pid2, "a", 26)  # 198.51.100.64/26
    _allocate(client, pid2, "b", 25)  # 198.51.100.128/25
    _create_plan(
        client,
        pid2,
        "p",
        [
            {"id": "x", "prefix": "198.51.100.64/26", "as_path": [10, 100]},
            {"id": "y", "prefix": "198.51.100.128/25", "as_path": [10, 100]},
        ],
    )
    _activate(client, pid2, "p")

    # Releasing b (.128/25), successor covers a (.64/26) but also the
    # excluded .0/26 via an overlapping prefix.
    resp = _decommission(
        client,
        pid2,
        request_id="b",
        active_plan_id="p",
        new_plan_id="q",
        announcements=[
            {"id": "x", "prefix": "198.51.100.0/25", "as_path": [10, 100]}
        ],
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_announcement_overlaps_exclusion"
    assert _plan_statuses(pid2) == {"p": "active"}


def test_non_valid_successor_announcement_cannot_cover(client):
    pid = _seed_two_allocations_with_active_plan(client)

    # The only covering announcement has an AS loop, so the remaining
    # allocation is uncovered for coverage purposes.
    resp = _decommission(
        client,
        pid,
        announcements=[
            {"id": "loop", "prefix": "203.0.113.0/25", "as_path": [10, 100, 10]}
        ],
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_allocation_uncovered"


def test_unknown_relation_edge_is_a_request_level_422(client):
    pid = _seed_two_allocations_with_active_plan(client)
    resp = _decommission(
        client,
        pid,
        announcements=[
            {"id": "keep", "prefix": "203.0.113.0/25", "as_path": [10, 200]}
        ],
        relationships=[],
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "as_relation_unknown"
    assert _plan_statuses(pid) == {"plan-a": "active"}


def test_active_plan_must_be_the_pool_single_active_plan(client):
    pid = _seed_two_allocations_with_active_plan(client)

    # Naming a planned (not active) plan.
    _create_plan(
        client,
        pid,
        "plan-c",
        [{"id": "all", "prefix": "203.0.113.0/24", "as_path": [10, 100]}],
    )
    resp = _decommission(client, pid, active_plan_id="plan-c")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_handover_set_mismatch"
    assert _plan_statuses(pid) == {"plan-a": "active", "plan-c": "planned"}

    # Naming an unknown plan.
    resp = _decommission(client, pid, active_plan_id="nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "bgp_plan_not_found"


def test_active_plan_from_another_pool_is_not_found(client):
    p1 = _seed_two_allocations_with_active_plan(client)
    p2 = _create_pool(client, cidr="203.0.114.0/24")
    _allocate(client, p2, "x1", 25)
    _allocate(client, p2, "x2", 25)
    _create_plan(
        client,
        p2,
        "foreign",
        [{"id": "all", "prefix": "203.0.114.0/24", "as_path": [10, 100]}],
    )
    _activate(client, p2, "foreign")

    resp = _decommission(
        client,
        p1,
        request_id="order-2",
        active_plan_id="foreign",
        new_plan_id="plan-b",
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "bgp_plan_not_found"

    assert client.post("/pools/999/decommission", json={
        "request_id": "order-2",
        "active_plan_id": "plan-a",
        "new_plan_id": "plan-b",
        "relationships": RELS,
        "announcements": [
            {"id": "keep", "prefix": "203.0.113.0/25", "as_path": [10, 100]}
        ],
    }).status_code == 404


def test_stale_active_plan_conflicts_and_changes_nothing(client):
    # A /23 so three allocations fit; the active plan is pinned while only
    # two /25s exist, then the pool drifts with a third allocation.
    pid = _create_pool(client, cidr="203.0.112.0/23")
    _allocate(client, pid, "order-1", 25)
    _allocate(client, pid, "order-2", 25)
    _create_plan(
        client,
        pid,
        "plan-a",
        [{"id": "all", "prefix": "203.0.112.0/23", "as_path": [10, 100]}],
    )
    _activate(client, pid, "plan-a")

    _allocate(client, pid, "order-3", 26)  # 203.0.113.0/26, uncovered drift

    resp = _decommission(
        client,
        pid,
        announcements=[
            {"id": "keep", "prefix": "203.0.112.0/25", "as_path": [10, 100]},
            {"id": "other", "prefix": "203.0.113.64/26", "as_path": [10, 100]},
            {"id": "other2", "prefix": "203.0.113.128/26", "as_path": [10, 100]},
        ],
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_state_conflict"

    # Neither the target nor the unrelated new allocation is touched.
    cidrs = {a["cidr"] for a in client.get(f"/pools/{pid}").json()["allocated"]}
    assert cidrs == {
        "203.0.112.0/25",
        "203.0.112.128/25",
        "203.0.113.0/26",
    }
    assert _plan_statuses(pid) == {"plan-a": "active"}


def test_new_plan_id_must_be_new(client):
    pid = _seed_two_allocations_with_active_plan(client)
    _create_plan(
        client,
        pid,
        "plan-c",
        [{"id": "all", "prefix": "203.0.113.0/24", "as_path": [10, 100]}],
    )

    resp = _decommission(client, pid, new_plan_id="plan-c")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_id_conflict"
    assert _plan_statuses(pid) == {"plan-a": "active", "plan-c": "planned"}

    # Same id as the active plan is a request-shape error.
    resp = _decommission(client, pid, new_plan_id="plan-a")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_decommission_invalid"


def test_target_allocation_must_exist_in_pool(client):
    pid = _seed_two_allocations_with_active_plan(client)
    resp = _decommission(client, pid, request_id="missing")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "allocation_not_found"
    assert _plan_statuses(pid) == {"plan-a": "active"}


def test_pool_must_keep_other_allocations(client):
    pid = _create_pool(client)
    _allocate(client, pid, "only", 24)
    _create_plan(
        client,
        pid,
        "plan-a",
        [{"id": "all", "prefix": "203.0.113.0/24", "as_path": [10, 100]}],
    )
    _activate(client, pid, "plan-a")

    resp = _decommission(
        client,
        pid,
        request_id="only",
        announcements=[
            {"id": "keep", "prefix": "203.0.113.0/25", "as_path": [10, 100]}
        ],
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "decommission_no_remaining_allocations"
    # Nothing changed.
    assert [a["cidr"] for a in client.get(f"/pools/{pid}").json()["allocated"]] == [
        "203.0.113.0/24"
    ]
    assert _plan_statuses(pid) == {"plan-a": "active"}


def test_decommission_is_not_replayable(client):
    pid = _seed_two_allocations_with_active_plan(client)
    assert _decommission(client, pid).status_code == 200

    # Repeat: plan-a is retired now (active set is {plan-b}), and the
    # released allocation is gone.
    resp = _decommission(client, pid)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_handover_set_mismatch"

    # Pointing at the new active plan with a fresh successor id fails
    # because the released allocation no longer exists.
    resp = _decommission(client, pid, active_plan_id="plan-b", new_plan_id="plan-c")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "allocation_not_found"


def test_retired_old_plan_cannot_be_reactivated_and_lifecycle_continues(client):
    pid = _seed_two_allocations_with_active_plan(client)
    assert _decommission(client, pid).status_code == 200

    resp = client.post(f"/pools/{pid}/bgp-plans/plan-a/activate")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_retired"

    # The active successor can hand over to another planned plan normally.
    remaining = [
        {"id": "keep", "prefix": "203.0.113.0/25", "as_path": [10, 100]}
    ]
    _create_plan(client, pid, "plan-c", remaining)
    resp = client.post(
        f"/pools/{pid}/bgp-plans/handover",
        json={"old_plan_ids": ["plan-b"], "new_plan_id": "plan-c"},
    )
    assert resp.status_code == 200, resp.text
    assert _plan_statuses(pid) == {
        "plan-a": "retired",
        "plan-b": "retired",
        "plan-c": "active",
    }


def test_decommission_keeps_provision_history_and_replay_does_not_reoccupy(client):
    pid = _create_pool(client)
    # Allocate an ordinary subnet first, then provision the second subnet
    # alongside announcements covering the existing allocation, so the
    # provision's plan is valid and can be activated over both.
    _allocate(client, pid, "plain-2", 25)  # 203.0.113.0/25
    body = {
        "request_id": "prov-1",
        "prefix_length": 25,
        "plan_id": "release-1",
        "relationships": RELS,
        "announcements": [
            {"id": "cur", "prefix": "203.0.113.0/25", "as_path": [10, 100]}
        ],
        "new_announcement": {"id": "new", "as_path": [10, 100]},
    }
    resp = client.post(f"/pools/{pid}/provisions", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["allocation"]["cidr"] == "203.0.113.128/25"
    client.post(f"/pools/{pid}/bgp-plans/release-1/activate")

    # Decommission the provisioned allocation while the ordinary one stays.
    resp = _decommission(
        client,
        pid,
        request_id="prov-1",
        active_plan_id="release-1",
        new_plan_id="release-2",
        announcements=[
            {"id": "cur", "prefix": "203.0.113.0/25", "as_path": [10, 100]}
        ],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["released"] == "203.0.113.128/25"
    assert _plan_statuses(pid) == {"release-1": "retired", "release-2": "active"}

    # The released address shows as free in the pool.
    free = client.get(f"/pools/{pid}").json()["free_cidrs"]
    assert "203.0.113.128/25" in free

    # Replaying the original provision returns the stored historical
    # allocation (same row id and CIDR), replayed: true, and does NOT
    # re-occupy the released address.
    replay = client.post(f"/pools/{pid}/provisions", json=body)
    assert replay.status_code == 200, replay.text
    rb = replay.json()
    assert rb["replayed"] is True
    assert rb["allocation"]["replayed"] is True
    assert rb["allocation"]["cidr"] == "203.0.113.128/25"
    assert rb["plan"]["plan_id"] == "release-1"
    assert rb["plan"]["status"] == "retired"

    status = client.get(f"/pools/{pid}").json()
    assert [a["cidr"] for a in status["allocated"]] == ["203.0.113.0/25"]
    assert "203.0.113.128/25" in status["free_cidrs"]

    # The provision record itself is still in the database.
    from app import db as db_module

    conn = sqlite3.connect(db_module.DB_PATH)
    try:
        row = conn.execute(
            "SELECT plan_id, request_id FROM address_provisions"
        ).fetchall()
    finally:
        conn.close()
    assert row == [("release-1", "prov-1")]


def test_concurrent_decommission_only_one_commits(client):
    # Two concurrent decommissions of the same active plan (releasing
    # different allocations, using the same successor id) cannot both
    # commit: BEGIN IMMEDIATE serialises them and the loser sees the
    # changed active set / occupied successor id.
    pid = _create_pool(client, cidr="192.0.2.0/24")
    _allocate(client, pid, "a1", 25)
    _allocate(client, pid, "a2", 25)
    _create_plan(
        client,
        pid,
        "act",
        [{"id": "all", "prefix": "192.0.2.0/24", "as_path": [10, 100]}],
    )
    _activate(client, pid, "act")

    def _succ(request_id):
        # Announcement covering exactly the allocation that stays.
        keep = "192.0.2.128/25" if request_id == "a1" else "192.0.2.0/25"
        return [{"id": "keep", "prefix": keep, "as_path": [10, 100]}]

    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()
    from app.main import app

    def worker(request_id):
        local = TestClient(app)
        barrier.wait()
        resp = local.post(
            f"/pools/{pid}/decommission",
            json={
                "request_id": request_id,
                "active_plan_id": "act",
                "new_plan_id": "succ",
                "relationships": RELS,
                "announcements": _succ(request_id),
            },
        )
        with lock:
            outcomes.append((resp.status_code, resp.json()))

    threads = [
        threading.Thread(target=worker, args=("a1",)),
        threading.Thread(target=worker, args=("a2",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(code for code, _ in outcomes)
    assert statuses == [200, 409]
    # Exactly one allocation released, exactly one plan cutover.
    assert _plan_statuses(pid) == {"act": "retired", "succ": "active"}
    remaining = {a["request_id"] for a in client.get(f"/pools/{pid}").json()["allocated"]}
    assert len(remaining) == 1
