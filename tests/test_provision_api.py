"""API integration tests for one-shot address provisioning with a BGP plan."""

from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

RELATIONSHIPS = [
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


def _body(
    request_id="order-1",
    prefix_length=25,
    plan_id="plan-1",
    relationships=None,
    announcements=None,
    new_id="new",
    new_path=(10, 100),
):
    return {
        "request_id": request_id,
        "prefix_length": prefix_length,
        "plan_id": plan_id,
        "relationships": RELATIONSHIPS if relationships is None else relationships,
        "announcements": [] if announcements is None else announcements,
        "new_announcement": {"id": new_id, "as_path": list(new_path)},
    }


def _provision(client, pool_id, body):
    return client.post(f"/pools/{pool_id}/provisions", json=body)


def test_provision_allocates_and_plans_in_one_call(client):
    pid = _create_pool(client)
    resp = _provision(client, pid, _body())
    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["replayed"] is False
    alloc = data["allocation"]
    assert alloc["cidr"] == "203.0.113.0/25"
    assert alloc["request_id"] == "order-1"
    assert alloc["prefix_length"] == 25
    assert alloc["replayed"] is False

    plan = data["plan"]
    assert plan["plan_id"] == "plan-1"
    assert plan["pool_id"] == pid
    assert plan["status"] == "planned"
    assert plan["activated_at"] is None
    assert plan["replayed"] is False
    assert [a["cidr"] for a in plan["pool_state"]["allocations"]] == [
        "203.0.113.0/25"
    ]
    assert len(plan["state_digest"]) == 64

    new_ann = data["new_announcement"]
    assert new_ann["id"] == "new"
    assert new_ann["prefix"] == "203.0.113.0/25"
    assert new_ann["as_path"] == [10, 100]
    assert new_ann["origin"] == 100
    assert new_ann["status"] == "valid"

    # The allocation is a regular allocation, visible to the status endpoint.
    status = client.get(f"/pools/{pid}").json()
    assert [a["cidr"] for a in status["allocated"]] == ["203.0.113.0/25"]
    assert status["free_cidrs"] == ["203.0.113.128/25"]


def test_provision_covers_existing_allocations(client):
    pid = _create_pool(client)
    _allocate(client, pid, "existing", 25)  # 203.0.113.0/25
    body = _body(
        request_id="order-2",
        announcements=[
            {"id": "cur", "prefix": "203.0.113.0/25", "as_path": [10, 100]}
        ],
        new_path=(100, 50, 60),
    )
    resp = _provision(client, pid, body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["allocation"]["cidr"] == "203.0.113.128/25"
    assert data["new_announcement"]["prefix"] == "203.0.113.128/25"
    assert data["new_announcement"]["origin"] == 60
    # The stored plan pins both allocations.
    assert [
        a["cidr"] for a in data["plan"]["pool_state"]["allocations"]
    ] == ["203.0.113.0/25", "203.0.113.128/25"]
    assert {r["id"] for r in data["plan"]["results"]} == {"cur", "new"}


def test_provision_selects_lowest_aligned_free(client):
    pid = _create_pool(client)
    # Occupy 203.0.113.0/26: the lowest aligned /25 then starts at .128
    # (the .64/26 remainder is not /25-aligned).
    _allocate(client, pid, "a", 26)
    body = _body(
        request_id="b",
        prefix_length=25,
        announcements=[
            {"id": "cur", "prefix": "203.0.113.0/26", "as_path": [10, 100]}
        ],
        new_path=(100, 50, 60),
    )
    resp = _provision(client, pid, body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["allocation"]["cidr"] == "203.0.113.128/25"


def test_provision_replay_same_params_returns_original(client):
    pid = _create_pool(client)
    first = _provision(client, pid, _body())
    assert first.status_code == 201
    second = _provision(client, pid, _body())
    assert second.status_code == 200, second.text
    replay = second.json()
    assert replay["replayed"] is True
    assert replay["allocation"]["replayed"] is True
    assert replay["plan"]["replayed"] is True
    assert replay["allocation"]["id"] == first.json()["allocation"]["id"]
    assert replay["allocation"]["cidr"] == first.json()["allocation"]["cidr"]
    assert replay["plan"]["created_at"] == first.json()["plan"]["created_at"]

    # Only one allocation and one plan exist.
    status = client.get(f"/pools/{pid}").json()
    assert len(status["allocated"]) == 1


def test_provision_replay_after_activation_reflects_active(client):
    pid = _create_pool(client)
    _provision(client, pid, _body())
    activated = client.post(f"/pools/{pid}/bgp-plans/plan-1/activate")
    assert activated.status_code == 200, activated.text
    replay = _provision(client, pid, _body())
    assert replay.status_code == 200
    assert replay.json()["plan"]["status"] == "active"
    assert replay.json()["plan"]["activated_at"] is not None


def test_provision_replay_changed_allocation_params_conflicts(client):
    pid = _create_pool(client)
    assert _provision(client, pid, _body()).status_code == 201
    changed = _body(prefix_length=26)
    resp = _provision(client, pid, changed)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "provision_id_conflict"


def test_provision_replay_changed_plan_params_conflicts(client):
    pid = _create_pool(client)
    assert _provision(client, pid, _body()).status_code == 201
    changed = _body(new_path=(100, 50, 60))
    resp = _provision(client, pid, changed)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "provision_id_conflict"


def test_provision_replay_changed_request_id_conflicts(client):
    pid = _create_pool(client)
    assert _provision(client, pid, _body()).status_code == 201
    changed = _body(request_id="other-order")
    resp = _provision(client, pid, changed)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "provision_id_conflict"


def test_request_id_taken_by_standalone_allocation_cannot_be_taken_over(client):
    pid = _create_pool(client)
    _allocate(client, pid, "order-1", 25)
    resp = _provision(client, pid, _body())
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "request_id_conflict"
    # No plan was left behind.
    plans = client.get(f"/pools/{pid}").json()
    assert len(plans["allocated"]) == 1


def test_plan_id_taken_by_standalone_plan_cannot_be_taken_over(client):
    pid = _create_pool(client)
    _allocate(client, pid, "existing", 25)
    plan_body = {
        "plan_id": "plan-1",
        "relationships": [
            {"type": "customer_provider", "customer": 10, "provider": 100}
        ],
        "announcements": [
            {"id": "a0", "prefix": "203.0.113.0/25", "as_path": [10, 100]}
        ],
    }
    assert client.post(f"/pools/{pid}/bgp-plans", json=plan_body).status_code == 201

    resp = _provision(
        client,
        pid,
        _body(
            request_id="order-1",
            relationships=[{"type": "customer_provider", "customer": 10, "provider": 100}],
            new_path=(10, 100),
        ),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_id_conflict"
    # The allocation was rolled back too.
    status = client.get(f"/pools/{pid}").json()
    assert [a["request_id"] for a in status["allocated"]] == ["existing"]


def test_provision_pool_exhausted_leaves_nothing(client):
    pid = _create_pool(client, cidr="203.0.113.0/25")
    _allocate(client, pid, "full", 25)
    resp = _provision(client, pid, _body(request_id="order-1"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "pool_exhausted"
    # Neither the allocation nor the plan exists; keys are reusable on a
    # fresh pool.
    status = client.get(f"/pools/{pid}").json()
    assert [a["request_id"] for a in status["allocated"]] == ["full"]
    other = _create_pool(client, cidr="198.51.100.0/25")
    assert _provision(client, other, _body(request_id="order-1")).status_code == 201


def test_provision_invalid_new_announcement_leaves_nothing(client):
    pid = _create_pool(client)
    # Empty AS path -> request validation 422.
    body = _body()
    body["new_announcement"] = {"id": "new", "as_path": []}
    resp = _provision(client, pid, body)
    assert resp.status_code == 422

    # Unknown relationship edge in the completed path -> 422, no partial state.
    resp = _provision(client, pid, _body(new_path=(10, 200)))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "as_relation_unknown"
    status = client.get(f"/pools/{pid}").json()
    assert status["allocated"] == []
    # Keys are not occupied.
    assert _provision(client, pid, _body()).status_code == 201


def test_provision_new_announcement_id_duplicate_is_422(client):
    pid = _create_pool(client)
    body = _body(
        announcements=[
            {"id": "dup", "prefix": "203.0.113.128/25", "as_path": [100, 50, 60]}
        ],
        new_id="dup",
        new_path=(10, 100),
    )
    resp = _provision(client, pid, body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "duplicate_announcement_id"
    assert client.get(f"/pools/{pid}").json()["allocated"] == []


def test_provision_origin_conflict_rolls_back(client):
    pid = _create_pool(client)
    _allocate(client, pid, "existing", 25)  # 203.0.113.0/25
    # Existing announcement origin 100 covers .0/25; the new /25 announcement
    # is valid for .128/25 -- both allocations covered by distinct origins is
    # fine.  Force a conflict by also covering the new block with origin 100
    # while the new announcement has origin 60: the new allocation then has
    # two covering announcements with different origins.
    body = _body(
        request_id="order-2",
        announcements=[
            {"id": "cur0", "prefix": "203.0.113.0/25", "as_path": [10, 100]},
            {"id": "whole", "prefix": "203.0.113.0/24", "as_path": [10, 100]},
        ],
        new_path=(100, 50, 60),
    )
    resp = _provision(client, pid, body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_allocation_origin_conflict"
    status = client.get(f"/pools/{pid}").json()
    assert [a["request_id"] for a in status["allocated"]] == ["existing"]


def test_provision_uncovered_existing_allocation_rolls_back(client):
    pid = _create_pool(client)
    _allocate(client, pid, "existing", 25)  # 203.0.113.0/25
    # No current announcement covers .0/25; the new announcement only covers
    # the newly allocated .128/25.
    resp = _provision(client, pid, _body(request_id="order-2", new_path=(100, 50, 60)))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_allocation_uncovered"
    status = client.get(f"/pools/{pid}").json()
    assert [a["request_id"] for a in status["allocated"]] == ["existing"]


def test_provision_announcement_overlapping_exclusion_rolls_back(client):
    pid = _create_pool(client, exclusions=["203.0.113.0/26"])
    body = _body(
        announcements=[
            {"id": "bad", "prefix": "203.0.113.0/26", "as_path": [10, 100]}
        ],
        new_path=(10, 100),
    )
    resp = _provision(client, pid, body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_announcement_overlaps_exclusion"
    assert client.get(f"/pools/{pid}").json()["allocated"] == []


def test_provision_created_plan_activates_with_fresh_state(client):
    pid = _create_pool(client)
    _provision(client, pid, _body())
    resp = client.post(f"/pools/{pid}/bgp-plans/plan-1/activate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"


def test_provision_created_plan_blocks_conflicting_later_allocation_activation(client):
    pid = _create_pool(client)
    _provision(client, pid, _body())
    # Pool drifts after provisioning: the pinned plan must fail activation.
    _allocate(client, pid, "later", 25)
    resp = client.post(f"/pools/{pid}/bgp-plans/plan-1/activate")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_state_conflict"


def test_provision_standalone_endpoints_still_work_alongside(client):
    pid = _create_pool(client)
    _provision(client, pid, _body())

    # The provisioned plan activates normally while the pool is still fresh.
    assert client.post(
        f"/pools/{pid}/bgp-plans/plan-1/activate"
    ).status_code == 200

    # Standalone allocation into the remaining /25.
    cidr = _allocate(client, pid, "standalone", 25)
    assert cidr == "203.0.113.128/25"

    # A standalone plan covering both allocations can be created (different
    # plan_id) and handed over from the active provisioned plan.
    plan_body = {
        "plan_id": "plan-2",
        "relationships": [
            {"type": "customer_provider", "customer": 10, "provider": 100},
            {"type": "peer", "a": 100, "b": 50},
            {"type": "customer_provider", "customer": 60, "provider": 50},
        ],
        "announcements": [
            {"id": "a0", "prefix": "203.0.113.0/25", "as_path": [10, 100]},
            {"id": "a128", "prefix": "203.0.113.128/25", "as_path": [100, 50, 60]},
        ],
    }
    assert client.post(f"/pools/{pid}/bgp-plans", json=plan_body).status_code == 201
    handover = client.post(
        f"/pools/{pid}/bgp-plans/handover",
        json={"old_plan_ids": ["plan-1"], "new_plan_id": "plan-2"},
    )
    assert handover.status_code == 200, handover.text
    statuses = {p["plan_id"]: p["status"] for p in handover.json()["plans"]}
    assert statuses == {"plan-1": "retired", "plan-2": "active"}

    # The provision replay now surfaces the retired status.
    replay = _provision(client, pid, _body())
    assert replay.status_code == 200
    assert replay.json()["plan"]["status"] == "retired"


def test_provision_unknown_pool_is_404(client):
    resp = _provision(client, 999, _body())
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "pool_not_found"


def test_concurrent_provisions_are_serialised_and_non_overlapping(client):
    pid = _create_pool(client)
    cidrs: list[str] = []
    errors: list[int] = []
    barrier = threading.Barrier(16)

    from app.main import app

    def worker(i: int):
        local = TestClient(app)
        barrier.wait()  # maximise contention
        # Every announcement originates the whole pool, so a provision
        # committing after another one still covers every allocation.
        resp = local.post(
            f"/pools/{pid}/provisions",
            json={
                "request_id": f"order-{i}",
                "prefix_length": 28,
                "plan_id": f"plan-{i}",
                "relationships": [
                    {"type": "customer_provider", "customer": 10, "provider": 100}
                ],
                "announcements": [
                    {"id": "pool", "prefix": "203.0.113.0/24", "as_path": [10, 100]}
                ],
                "new_announcement": {"id": f"new-{i}", "as_path": [10, 100]},
            },
        )
        if resp.status_code == 201:
            cidrs.append(resp.json()["allocation"]["cidr"])
        else:
            errors.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # A /24 holds exactly 16 aligned /28s, and each full-pool announcement
    # keeps coverage valid, so every provision commits.
    assert errors == []
    assert len(cidrs) == 16
    assert len(set(cidrs)) == 16


def test_concurrent_same_plan_id_provisions_commit_once(client):
    pid = _create_pool(client)
    statuses: list[int] = []
    cidrs: list[str] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    from app.main import app

    def worker():
        local = TestClient(app)
        barrier.wait()
        resp = local.post(
            f"/pools/{pid}/provisions",
            json={
                "request_id": "order-1",
                "prefix_length": 25,
                "plan_id": "plan-1",
                "relationships": [
                    {"type": "customer_provider", "customer": 10, "provider": 100}
                ],
                "announcements": [],
                "new_announcement": {"id": "new", "as_path": [10, 100]},
            },
        )
        with lock:
            statuses.append(resp.status_code)
            if resp.status_code in (200, 201):
                cidrs.append(resp.json()["allocation"]["cidr"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one real commit; all other writers replay it (200).
    assert statuses.count(201) == 1
    assert sorted(set(statuses)) == [200, 201]
    assert set(cidrs) == {"203.0.113.0/25"}
    status = client.get(f"/pools/{pid}").json()
    assert len(status["allocated"]) == 1


# ---------------------------------------------------------------------------
# Releasing a provisioned allocation must not break the plan_id retry
# ---------------------------------------------------------------------------


def _release(client, pool_id, request_id):
    resp = client.delete(f"/pools/{pool_id}/allocations/{request_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["released"]


def test_provision_replay_after_release_returns_original(client):
    pid = _create_pool(client)
    first = _provision(client, pid, _body())
    assert first.status_code == 201, first.text
    original = first.json()

    assert _release(client, pid, "order-1") == "203.0.113.0/25"

    # The released address is free again and absent from current allocations.
    status = client.get(f"/pools/{pid}").json()
    assert status["allocated"] == []
    assert status["free_cidrs"] == ["203.0.113.0/24"]

    # Same plan_id + same params: replay the stored result; no address is
    # taken again.
    replay = _provision(client, pid, _body())
    assert replay.status_code == 200, replay.text
    data = replay.json()
    assert data["replayed"] is True
    assert data["allocation"]["replayed"] is True
    assert data["plan"]["replayed"] is True
    assert data["allocation"]["id"] == original["allocation"]["id"]
    assert data["allocation"]["cidr"] == "203.0.113.0/25"
    assert data["allocation"]["request_id"] == "order-1"
    assert data["plan"]["plan_id"] == "plan-1"
    assert data["new_announcement"]["prefix"] == "203.0.113.0/25"

    # Still no current allocation, and the freed block stays fully free.
    status = client.get(f"/pools/{pid}").json()
    assert status["allocated"] == []
    assert status["free_cidrs"] == ["203.0.113.0/24"]


def test_provision_replay_after_release_reflects_current_plan_status(client):
    pid = _create_pool(client)
    assert _provision(client, pid, _body()).status_code == 201
    assert client.post(
        f"/pools/{pid}/bgp-plans/plan-1/activate"
    ).status_code == 200

    _release(client, pid, "order-1")
    replay = _provision(client, pid, _body())
    assert replay.status_code == 200, replay.text
    plan = replay.json()["plan"]
    assert plan["status"] == "active"
    assert plan["activated_at"] is not None


def test_released_request_id_can_be_reused_by_ordinary_allocation(client):
    pid = _create_pool(client)
    first = _provision(client, pid, _body())
    assert first.status_code == 201
    original_alloc_id = first.json()["allocation"]["id"]
    _release(client, pid, "order-1")

    # The freed request_id is reusable for an ordinary allocation.  The
    # lowest free block is the same CIDR, but it is a brand-new row.
    ordinary = client.post(
        f"/pools/{pid}/allocations",
        json={"request_id": "order-1", "prefix_length": 26},
    )
    assert ordinary.status_code == 201, ordinary.text
    new_alloc = ordinary.json()
    assert new_alloc["cidr"] == "203.0.113.0/26"
    assert new_alloc["replayed"] is False
    assert new_alloc["id"] != original_alloc_id

    # The old provision still replays by plan_id against its *original*
    # record, regardless of the request_id now pointing elsewhere.
    replay = _provision(client, pid, _body())
    assert replay.status_code == 200, replay.text
    data = replay.json()
    assert data["replayed"] is True
    assert data["allocation"]["id"] == original_alloc_id
    assert data["allocation"]["cidr"] == "203.0.113.0/25"
    assert data["allocation"]["request_id"] == "order-1"

    # The ordinary, current allocation is untouched and not the historical one.
    status = client.get(f"/pools/{pid}").json()
    assert [a["cidr"] for a in status["allocated"]] == ["203.0.113.0/26"]
    assert status["allocated"][0]["request_id"] == "order-1"


def test_changed_params_after_release_still_conflict(client):
    pid = _create_pool(client)
    assert _provision(client, pid, _body()).status_code == 201
    _release(client, pid, "order-1")

    # Different prefix_length with the same plan_id conflicts.
    resp = _provision(client, pid, _body(prefix_length=26))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "provision_id_conflict"

    # Different request_id with the same plan_id conflicts too.
    resp = _provision(client, pid, _body(request_id="other-order"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "provision_id_conflict"

    # A standalone plan cannot take over the still-recorded plan_id either.
    plan_resp = client.post(
        f"/pools/{pid}/bgp-plans",
        json={
            "plan_id": "plan-1",
            "relationships": RELATIONSHIPS,
            "announcements": [
                {"id": "whole", "prefix": "203.0.113.0/24", "as_path": [10, 100]}
            ],
        },
    )
    assert plan_resp.status_code == 409
    assert plan_resp.json()["error"]["code"] == "bgp_plan_id_conflict"

    # Nothing was allocated by the rejected attempts; the original replay is
    # still intact.
    assert client.get(f"/pools/{pid}").json()["allocated"] == []
    replay = _provision(client, pid, _body())
    assert replay.status_code == 200
    assert replay.json()["allocation"]["cidr"] == "203.0.113.0/25"


def test_different_plan_id_after_release_takes_the_freed_address(client):
    pid = _create_pool(client)
    assert _provision(client, pid, _body()).status_code == 201
    _release(client, pid, "order-1")

    # A genuinely new provision (new plan_id, new request_id) may take the
    # freed block, while the old plan_id keeps conflicting on different params.
    resp = _provision(
        client,
        pid,
        _body(request_id="order-2", plan_id="plan-2"),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["allocation"]["cidr"] == "203.0.113.0/25"
    assert resp.json()["new_announcement"]["prefix"] == "203.0.113.0/25"

    # The historical provision is unaffected and still replays verbatim.
    replay = _provision(client, pid, _body())
    assert replay.status_code == 200
    assert replay.json()["allocation"]["cidr"] == "203.0.113.0/25"
    assert replay.json()["plan"]["plan_id"] == "plan-1"

    # Only the current allocation shows up in the pool status.
    status = client.get(f"/pools/{pid}").json()
    assert [a["request_id"] for a in status["allocated"]] == ["order-2"]


def test_standalone_allocations_plans_and_handover_keep_working_after_release(client):
    pid = _create_pool(client)
    assert _provision(client, pid, _body()).status_code == 201
    assert client.post(
        f"/pools/{pid}/bgp-plans/plan-1/activate"
    ).status_code == 200
    _release(client, pid, "order-1")

    # An independent ordinary allocation goes into the freed space.
    cidr = _allocate(client, pid, "standalone", 25)
    assert cidr == "203.0.113.0/25"

    # A standalone plan can be created with a fresh plan_id and handed over
    # from the active provision-created plan.
    plan_body = {
        "plan_id": "plan-2",
        "relationships": RELATIONSHIPS,
        "announcements": [
            {"id": "new", "prefix": "203.0.113.0/25", "as_path": [10, 100]}
        ],
    }
    assert client.post(f"/pools/{pid}/bgp-plans", json=plan_body).status_code == 201
    handover = client.post(
        f"/pools/{pid}/bgp-plans/handover",
        json={"old_plan_ids": ["plan-1"], "new_plan_id": "plan-2"},
    )
    assert handover.status_code == 200, handover.text
    statuses = {p["plan_id"]: p["status"] for p in handover.json()["plans"]}
    assert statuses == {"plan-1": "retired", "plan-2": "active"}

    # The released provision replay surfaces the current (retired) status.
    replay = _provision(client, pid, _body())
    assert replay.status_code == 200
    assert replay.json()["plan"]["status"] == "retired"

    # The independent allocation remains current; the historical one does not.
    status = client.get(f"/pools/{pid}").json()
    assert [a["request_id"] for a in status["allocated"]] == ["standalone"]


# ---------------------------------------------------------------------------
# Schema migration of databases whose provisions cascaded with allocations
# ---------------------------------------------------------------------------

# The bgp_plans table as created before the handover feature existed.
_LEGACY_BGP_PLANS_DDL = """
CREATE TABLE bgp_plans (
    plan_id          TEXT    PRIMARY KEY,
    pool_id          INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    params           TEXT    NOT NULL,
    audit_results    TEXT    NOT NULL,
    pool_state       TEXT    NOT NULL,
    state_digest     TEXT    NOT NULL,
    status           TEXT    NOT NULL CHECK (status IN ('planned', 'active')),
    created_at       TEXT    NOT NULL,
    activated_at     TEXT
);
CREATE INDEX idx_bgp_plans_pool ON bgp_plans(pool_id);
"""

# The address_provisions table as created before released provisions had to
# stay replayable: allocation_id carried ON DELETE CASCADE.
_LEGACY_ADDRESS_PROVISIONS_DDL = """
CREATE TABLE address_provisions (
    plan_id        TEXT    PRIMARY KEY
                     REFERENCES bgp_plans(plan_id) ON DELETE CASCADE,
    pool_id        INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    allocation_id  INTEGER NOT NULL REFERENCES allocations(id) ON DELETE CASCADE,
    request_id     TEXT    NOT NULL,
    params         TEXT    NOT NULL,
    result         TEXT    NOT NULL,
    created_at     TEXT    NOT NULL
);
CREATE INDEX idx_address_provisions_request ON address_provisions(request_id);
"""


def _rewind_to_dual_legacy_db(db_path):
    """Rewind both tables to their pre-handover/pre-release definitions.

    A genuinely old database has the old DDL on disk with live rows; the
    rewrite keeps the rows by suspending foreign-key enforcement (otherwise
    dropping the renamed plans copy cascades the provisions away, which is
    exactly the upgrade bug under test).
    """

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            "ALTER TABLE bgp_plans RENAME TO bgp_plans_new;\n"
            "DROP INDEX idx_bgp_plans_pool;\n"
            + _LEGACY_BGP_PLANS_DDL
            + """
            INSERT INTO bgp_plans (plan_id, pool_id, params, audit_results,
                pool_state, state_digest, status, created_at, activated_at)
            SELECT plan_id, pool_id, params, audit_results, pool_state,
                state_digest, status, created_at, activated_at
            FROM bgp_plans_new;
            DROP TABLE bgp_plans_new;

            ALTER TABLE address_provisions RENAME TO address_provisions_new;
            DROP INDEX idx_address_provisions_request;
            """
            + _LEGACY_ADDRESS_PROVISIONS_DDL
            + """
            INSERT INTO address_provisions (plan_id, pool_id, allocation_id,
                request_id, params, result, created_at)
            SELECT plan_id, pool_id, allocation_id, request_id, params, result,
                created_at FROM address_provisions_new;
            DROP TABLE address_provisions_new;
            """
        )
        conn.commit()
        plans_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='bgp_plans'"
        ).fetchone()[0]
        provisions_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='address_provisions'"
        ).fetchone()[0]
        assert "'retired'" not in plans_sql
        assert "REFERENCES allocations" in provisions_sql
        assert conn.execute("SELECT count(*) FROM address_provisions").fetchone()[0] == 1
    finally:
        conn.close()


def test_legacy_provision_table_is_migrated_and_remains_replayable(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-provision.db"
    monkeypatch.setenv("SUBNET_DB_PATH", str(db_path))
    from app import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    db_module.init_db()

    from app.main import app

    with TestClient(app) as client:
        pid = _create_pool(client)
        first = _provision(client, pid, _body())
        assert first.status_code == 201, first.text
        original = first.json()
        allocation_id = original["allocation"]["id"]

    # Rewind the address_provisions table to its old cascading definition,
    # keeping the un-released provision row, to simulate a pre-fix database.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            "ALTER TABLE address_provisions RENAME TO address_provisions_new;\n"
            # The rename carries the old index with the table; drop it so the
            # legacy CREATE INDEX below reproduces a real pre-fix database.
            "DROP INDEX idx_address_provisions_request;\n"
            + _LEGACY_ADDRESS_PROVISIONS_DDL
            + """
            INSERT INTO address_provisions (plan_id, pool_id, allocation_id,
                request_id, params, result, created_at)
            SELECT plan_id, pool_id, allocation_id, request_id, params, result,
                created_at FROM address_provisions_new;
            DROP TABLE address_provisions_new;
            """
        )
        conn.commit()
        legacy_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='address_provisions'"
        ).fetchone()[0]
        assert "REFERENCES allocations" in legacy_sql
    finally:
        conn.close()

    # Upgrade: the table is rebuilt and the un-released provision row is
    # copied verbatim.
    db_module.init_db()

    conn = sqlite3.connect(str(db_path))
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='address_provisions'"
        ).fetchone()[0]
        assert "REFERENCES allocations" not in sql
        assert "REFERENCES bgp_plans" in sql
        row = conn.execute(
            "SELECT allocation_id, request_id, params, result "
            "FROM address_provisions WHERE plan_id = 'plan-1'"
        ).fetchone()
        assert row[0] == allocation_id
        assert row[1] == "order-1"
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='address_provisions'"
            )
        }
        assert "idx_address_provisions_request" in indexes
    finally:
        conn.close()

    # The un-released legacy provision still replays after the upgrade...
    with TestClient(app) as client:
        replay = _provision(client, pid, _body())
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["allocation"]["id"] == allocation_id
        assert replay.json()["allocation"]["cidr"] == "203.0.113.0/25"
        assert replay.json()["plan"]["status"] == "planned"

        # ...and once upgraded, releasing it no longer drops the record, so
        # the identical retry keeps returning the original result while the
        # freed address stays out of the pool's current allocations.
        assert _release(client, pid, "order-1") == "203.0.113.0/25"
        replay_after_release = _provision(client, pid, _body())
        assert replay_after_release.status_code == 200
        data = replay_after_release.json()
        assert data["replayed"] is True
        assert data["allocation"]["id"] == allocation_id
        assert data["allocation"]["cidr"] == "203.0.113.0/25"
        assert client.get(f"/pools/{pid}").json()["allocated"] == []


# ---------------------------------------------------------------------------
# Coordinated upgrade of a database predating BOTH the retired status and
# the non-cascading allocation_id
# ---------------------------------------------------------------------------


def test_dual_legacy_database_keeps_provisions_on_upgrade(tmp_path, monkeypatch):
    db_path = tmp_path / "dual-legacy.db"
    monkeypatch.setenv("SUBNET_DB_PATH", str(db_path))
    from app import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    db_module.init_db()

    from app.main import app

    with TestClient(app) as client:
        pid = _create_pool(client)
        first = _provision(client, pid, _body())
        assert first.status_code == 201, first.text
        original = first.json()
        allocation_id = original["allocation"]["id"]
        assert original["plan"]["status"] == "planned"

    # Simulate a database created before both features existed: bgp_plans
    # only knows planned/active, and address_provisions.allocation_id
    # cascades.  The un-released provision row stays in place.
    _rewind_to_dual_legacy_db(db_path)

    # The upgrade rebuilds both tables in one coordinated transaction and
    # preserves the identity and linkage of every row.
    db_module.init_db()

    conn = sqlite3.connect(str(db_path))
    try:
        plans_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='bgp_plans'"
        ).fetchone()[0]
        provisions_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='address_provisions'"
        ).fetchone()[0]
        assert "'retired'" in plans_sql
        assert "REFERENCES allocations" not in provisions_sql
        assert "REFERENCES bgp_plans" in provisions_sql

        plans = conn.execute("SELECT plan_id, status FROM bgp_plans").fetchall()
        assert plans == [("plan-1", "planned")]
        provision = conn.execute(
            "SELECT plan_id, pool_id, allocation_id, request_id "
            "FROM address_provisions"
        ).fetchone()
        assert tuple(provision) == ("plan-1", pid, allocation_id, "order-1")
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name IN ('bgp_plans', 'address_provisions')"
            )
        }
        assert "idx_bgp_plans_pool" in indexes
        assert "idx_address_provisions_request" in indexes
    finally:
        conn.close()

    # The original request replays its historical allocation and announcement
    # together with the plan's current status; it is not mistaken for a fresh
    # request_id collision (the pre-fix bug).
    with TestClient(app) as client:
        replay = _provision(client, pid, _body())
        assert replay.status_code == 200, replay.text
        data = replay.json()
        assert data["replayed"] is True
        assert data["allocation"]["id"] == allocation_id
        assert data["allocation"]["cidr"] == "203.0.113.0/25"
        assert data["new_announcement"]["prefix"] == "203.0.113.0/25"
        assert data["plan"]["plan_id"] == "plan-1"
        assert data["plan"]["status"] == "planned"

        # The plan is still an ordinary bgp_plans row: activate it, hand it
        # over to a successor, and the subsequent replay surfaces the current
        # retired status.
        activate = client.post(f"/pools/{pid}/bgp-plans/plan-1/activate")
        assert activate.status_code == 200, activate.text
        plan_body = {
            "plan_id": "plan-2",
            "relationships": [
                {"type": "customer_provider", "customer": 10, "provider": 100}
            ],
            "announcements": [
                {"id": "new", "prefix": "203.0.113.0/25", "as_path": [10, 100]}
            ],
        }
        assert (
            client.post(f"/pools/{pid}/bgp-plans", json=plan_body).status_code
            == 201
        )
        handover = client.post(
            f"/pools/{pid}/bgp-plans/handover",
            json={"old_plan_ids": ["plan-1"], "new_plan_id": "plan-2"},
        )
        assert handover.status_code == 200, handover.text
        replay_after_handover = _provision(client, pid, _body())
        assert replay_after_handover.status_code == 200
        assert replay_after_handover.json()["replayed"] is True
        assert replay_after_handover.json()["plan"]["status"] == "retired"

        # Release then same-parameters retry: the stored record replays and
        # the freed address is never re-occupied.
        assert _release(client, pid, "order-1") == "203.0.113.0/25"
        replay_after_release = _provision(client, pid, _body())
        assert replay_after_release.status_code == 200
        data = replay_after_release.json()
        assert data["replayed"] is True
        assert data["allocation"]["id"] == allocation_id
        assert data["allocation"]["cidr"] == "203.0.113.0/25"
        assert client.get(f"/pools/{pid}").json()["allocated"] == []


def test_dual_legacy_upgrade_is_idempotent_and_failure_leaves_legacy_intact(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "dual-legacy-atomic.db"
    monkeypatch.setenv("SUBNET_DB_PATH", str(db_path))
    from app import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    db_module.init_db()

    from app.main import app

    with TestClient(app) as client:
        pid = _create_pool(client)
        first = _provision(client, pid, _body())
        assert first.status_code == 201, first.text
        allocation_id = first.json()["allocation"]["id"]

    _rewind_to_dual_legacy_db(db_path)

    # Break the plans rebuild statement (the migration reads this DDL
    # constant): the rename succeeds inside the transaction, then the CREATE
    # fails.  The coordinated rebuild must roll the rename back as well.
    original_ddl = db_module._BGP_PLANS_DDL
    db_module._BGP_PLANS_DDL = "CREATE TABLE bgp_plans (bad"

    with pytest.raises(sqlite3.Error):
        db_module.init_db()
    db_module._BGP_PLANS_DDL = original_ddl

    conn = sqlite3.connect(str(db_path))
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "bgp_plans" in names
        assert "address_provisions" in names
        assert "bgp_plans_legacy" not in names
        assert "address_provisions_legacy" not in names

        plans_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='bgp_plans'"
        ).fetchone()[0]
        provisions_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='address_provisions'"
        ).fetchone()[0]
        assert "'retired'" not in plans_sql
        assert "REFERENCES allocations" in provisions_sql
        assert conn.execute("SELECT count(*) FROM bgp_plans").fetchone()[0] == 1
        assert (
            conn.execute("SELECT count(*) FROM address_provisions").fetchone()[0]
            == 1
        )
    finally:
        conn.close()

    # The retry completes the upgrade; an immediately repeated init is a
    # no-op that keeps the records.
    db_module.init_db()
    db_module.init_db()

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT plan_id, status FROM bgp_plans").fetchone() == (
            "plan-1",
            "planned",
        )
        assert tuple(
            conn.execute(
                "SELECT plan_id, allocation_id, request_id FROM address_provisions"
            ).fetchone()
        ) == ("plan-1", allocation_id, "order-1")
    finally:
        conn.close()
