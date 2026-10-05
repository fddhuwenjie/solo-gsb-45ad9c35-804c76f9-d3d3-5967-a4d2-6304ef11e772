"""Cross-pool allocation migration: atomically move in-use subnets between pools."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient


def _create_pool(client, cidr, exclusions=None):
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
    return resp.json()


def _release(client, pool_id, request_id):
    resp = client.delete(f"/pools/{pool_id}/allocations/{request_id}")
    assert resp.status_code == 200, resp.text
    return resp


def _migrate(client, source_id, target_id, operation_id, request_ids):
    return client.post(
        f"/pools/{source_id}/allocation-migrations",
        json={
            "operation_id": operation_id,
            "target_pool_id": target_id,
            "allocations": [{"request_id": rid} for rid in request_ids],
        },
    )


def _allocated(client, pool_id) -> dict[str, dict]:
    return {x["request_id"]: x for x in client.get(f"/pools/{pool_id}").json()["allocated"]}


def test_migration_moves_allocations_preserving_identity(client):
    src = _create_pool(client, "10.0.0.0/24")
    # Target contains the source CIDR (pools may overlap) and already has an
    # allocation elsewhere in its space.  Allocate-then-release carves the
    # target's existing allocation at 10.0.64.0/18 so it avoids the /24.
    dst = _create_pool(client, "10.0.0.0/16")
    _allocate(client, dst, "hold", 18)      # 10.0.0.0/18
    taken = _allocate(client, dst, "req-taken", 18)  # 10.0.64.0/18
    _release(client, dst, "hold")

    a = _allocate(client, src, "req-a", 25)  # 10.0.0.0/25
    b = _allocate(client, src, "req-b", 25)  # 10.0.0.128/25

    resp = _migrate(client, src, dst, "move-1", ["req-b", "req-a"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["operation_id"] == "move-1"
    assert body["source_pool_id"] == src
    assert body["target_pool_id"] == dst
    assert body["source_cidr"] == "10.0.0.0/24"
    assert body["target_cidr"] == "10.0.0.0/16"
    assert body["replayed"] is False
    assert isinstance(body["migration_id"], int)

    # The mapping follows input order, not CIDR or id order.
    moved = body["migrated"]
    assert [m["request_id"] for m in moved] == ["req-b", "req-a"]
    assert [m["cidr"] for m in moved] == ["10.0.0.128/25", "10.0.0.0/25"]
    assert [m["prefix_length"] for m in moved] == [25, 25]
    assert {m["allocation_id"] for m in moved} == {a["id"], b["id"]}
    assert all(m["replayed"] is False for m in moved)

    # Both pools still exist; the source kept no allocations.
    assert client.get(f"/pools/{src}").json()["allocated"] == []
    # The moved CIDRs were preserved verbatim (not re-picked from the target
    # free space) and sit alongside the target's own allocation.
    target_allocs = _allocated(client, dst)
    assert set(target_allocs) == {"req-a", "req-b", "req-taken"}
    assert target_allocs["req-a"]["cidr"] == "10.0.0.0/25"
    assert target_allocs["req-b"]["cidr"] == "10.0.0.128/25"
    assert target_allocs["req-taken"]["cidr"] == taken["cidr"]

    # Row identity survived: replaying the request_id against the target pool
    # returns the original row id, and release works on the new owner.
    replay = client.post(
        f"/pools/{dst}/allocations",
        json={"request_id": "req-a", "prefix_length": 25},
    )
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    assert replay.json()["id"] == a["id"]
    assert replay.json()["cidr"] == "10.0.0.0/25"

    freed = client.delete(f"/pools/{dst}/allocations/req-b")
    assert freed.status_code == 200
    assert freed.json()["released"] == "10.0.0.128/25"


def test_migration_works_between_partially_overlapping_pools(client):
    # Pools may overlap by construction; containment in the target is what
    # matters, not source/target pool containment.
    src = _create_pool(client, "10.0.0.0/24")
    dst = _create_pool(client, "10.0.0.128/25")
    a = _allocate(client, src, "req-a", 25)  # 10.0.0.0/25: not in target
    b = _allocate(client, src, "req-b", 26)  # 10.0.0.128/26: in target

    resp = _migrate(client, src, dst, "move-partial", ["req-a"])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "migration_allocation_outside_target"
    # Nothing moved on the failed attempt.
    assert _allocated(client, src) != {}

    resp = _migrate(client, src, dst, "move-ok", ["req-b"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["migrated"][0]["cidr"] == "10.0.0.128/26"
    assert resp.json()["migrated"][0]["allocation_id"] == b["id"]
    assert set(_allocated(client, dst)) == {"req-b"}
    assert set(_allocated(client, src)) == {"req-a"}
    assert a["id"] != b["id"]


def test_migration_replay_returns_original_result(client):
    src = _create_pool(client, "10.0.0.0/24")
    dst = _create_pool(client, "10.0.0.0/16")
    _allocate(client, src, "req-a", 26)
    _allocate(client, src, "req-b", 26)

    first = _migrate(client, src, dst, "op-idem", ["req-a", "req-b"])
    assert first.status_code == 201, first.text

    second = _migrate(client, src, dst, "op-idem", ["req-a", "req-b"])
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["replayed"] is True
    assert body["migration_id"] == first.json()["migration_id"]
    assert body["migrated"] == first.json()["migrated"]

    # The retry did not touch the rows a second time; the target still holds
    # exactly the two moved allocations.
    assert len(_allocated(client, dst)) == 2

    # Replay keeps working even after a migrated allocation is released: the
    # stored original result is returned verbatim.
    _release(client, dst, "req-a")
    third = _migrate(client, src, dst, "op-idem", ["req-a", "req-b"])
    assert third.status_code == 200
    assert third.json()["replayed"] is True
    assert third.json()["migrated"] == first.json()["migrated"]


def test_migration_conflict_when_manifest_order_changes(client):
    src = _create_pool(client, "10.0.0.0/24")
    dst = _create_pool(client, "10.0.0.0/16")
    _allocate(client, src, "req-a", 26)
    _allocate(client, src, "req-b", 26)

    assert _migrate(client, src, dst, "op-x", ["req-a", "req-b"]).status_code == 201

    resp = _migrate(client, src, dst, "op-x", ["req-b", "req-a"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "operation_id_conflict"
    # The original migration result is unaffected.
    replay = _migrate(client, src, dst, "op-x", ["req-a", "req-b"])
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True


def test_migration_conflict_when_pools_change(client):
    p1 = _create_pool(client, "10.0.0.0/24")
    p2 = _create_pool(client, "10.0.0.0/16")
    p3 = _create_pool(client, "10.0.0.0/8")
    _allocate(client, p1, "req-a", 26)
    _allocate(client, p1, "req-b", 26)
    # Carve p2's existing allocation at 10.0.0.64/26 so it avoids req-a.
    _allocate(client, p2, "hold-c", 26)
    _allocate(client, p2, "req-c", 26)
    _release(client, p2, "hold-c")

    assert _migrate(client, p1, p2, "op-shared", ["req-a"]).status_code == 201

    # Same operation_id, different source pool.
    resp = _migrate(client, p2, p3, "op-shared", ["req-c"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "operation_id_conflict"

    # Same operation_id, different target pool.
    resp = _migrate(client, p1, p3, "op-shared", ["req-b"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "operation_id_conflict"

    # Nothing else moved.
    assert set(_allocated(client, p2)) == {"req-a", "req-c"}
    assert set(_allocated(client, p1)) == {"req-b"}


def test_migration_missing_pool_404(client):
    src = _create_pool(client, "10.0.0.0/24")
    _allocate(client, src, "req-a", 26)
    resp = _migrate(client, src, 999, "op-404", ["req-a"])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "pool_not_found"

    resp = _migrate(client, 998, src, "op-404b", ["req-a"])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "pool_not_found"


def test_migration_same_pool_rejected(client):
    pid = _create_pool(client, "10.0.0.0/24")
    _allocate(client, pid, "req-a", 26)
    resp = _migrate(client, pid, pid, "op-self", ["req-a"])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "migration_same_pool"
    assert client.get(f"/pools/{pid}").status_code == 200


def test_migration_duplicate_manifest_entry_rejected(client):
    src = _create_pool(client, "10.0.0.0/24")
    dst = _create_pool(client, "10.0.0.0/16")
    _allocate(client, src, "req-a", 26)

    resp = _migrate(client, src, dst, "op-dup", ["req-a", "req-a"])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "migration_duplicate_request_id"
    assert set(_allocated(client, src)) == {"req-a"}
    assert _allocated(client, dst) == {}


def test_migration_unknown_request_id_404(client):
    src = _create_pool(client, "10.0.0.0/24")
    dst = _create_pool(client, "10.0.0.0/16")
    resp = _migrate(client, src, dst, "op-missing", ["nope"])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "allocation_not_found"


def test_migration_allocation_in_other_pool_rejected(client):
    src = _create_pool(client, "10.0.0.0/24")
    dst = _create_pool(client, "10.0.0.0/16")
    other = _create_pool(client, "10.2.0.0/16")
    _allocate(client, other, "foreign", 26)

    resp = _migrate(client, src, dst, "op-foreign", ["foreign"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "migration_allocation_not_in_source"
    # The foreign allocation keeps its original owner.
    assert client.delete(f"/pools/{other}/allocations/foreign").status_code == 200


def test_migration_blocked_by_planned_bgp_plan_on_either_pool(client):
    src = _create_pool(client, "203.0.113.0/24")
    # Target contains the source range; the exclusion sits outside it.
    dst = _create_pool(client, "203.0.112.0/20", exclusions=["203.0.114.0/24"])
    _allocate(client, src, "order-1", 25)
    relationships = [
        {"type": "customer_provider", "customer": 10, "provider": 100}
    ]

    def plan(pool_id, announcements):
        return client.post(
            f"/pools/{pool_id}/bgp-plans",
            json={
                "plan_id": f"plan-{pool_id}",
                "relationships": relationships,
                "announcements": announcements,
            },
        )

    # Planned plan on the source pool blocks the move.
    assert plan(
        src,
        [{"id": "a0", "prefix": "203.0.113.0/24", "as_path": [100]}],
    ).status_code == 201
    resp = _migrate(client, src, dst, "op-plan-src", ["order-1"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "migration_blocked_by_bgp_plan"
    assert _allocated(client, src) != {}

    # Active plan on the target pool blocks it as well.  The empty /25 target
    # has no allocations, so a plan covering its whole range validates and can
    # be activated; the moved /26 CIDR is contained in that same pool.
    src2 = _create_pool(client, "198.51.100.0/24")
    dst_with_plan = _create_pool(client, "198.51.100.128/25")
    _allocate(client, src2, "keep", 25)    # 198.51.100.0/25: not migrated
    _allocate(client, src2, "order-2", 26)  # 198.51.100.128/26: inside target
    assert plan(
        dst_with_plan,
        [{"id": "a128", "prefix": "198.51.100.128/25", "as_path": [100]}],
    ).status_code == 201
    activate = client.post(
        f"/pools/{dst_with_plan}/bgp-plans/plan-{dst_with_plan}/activate"
    )
    assert activate.status_code == 200
    resp = _migrate(client, src2, dst_with_plan, "op-plan-dst", ["order-2"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "migration_blocked_by_bgp_plan"
    assert set(_allocated(client, src2)) == {"order-2", "keep"}
    assert _allocated(client, dst_with_plan) == {}


def test_migration_rejected_when_cidr_overlaps_target_exclusion(client):
    src = _create_pool(client, "10.0.0.0/24")
    dst = _create_pool(client, "10.0.0.0/16", exclusions=["10.0.0.0/26"])
    a = _allocate(client, src, "req-a", 26)  # 10.0.0.0/26: excluded in target
    b = _allocate(client, src, "req-b", 26)  # 10.0.0.64/26: free in target

    resp = _migrate(client, src, dst, "op-excl", ["req-a"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "migration_allocation_overlaps_exclusion"

    # The other allocation is movable; the rejected attempt changed nothing.
    ok = _migrate(client, src, dst, "op-excl-ok", ["req-b"])
    assert ok.status_code == 201, ok.text
    assert set(_allocated(client, src)) == {"req-a"}
    assert a["cidr"] == "10.0.0.0/26"


def test_migration_rejected_when_cidr_overlaps_target_allocation(client):
    src = _create_pool(client, "10.0.0.0/24")
    dst = _create_pool(client, "10.0.0.0/16")
    _allocate(client, src, "req-move", 25)    # 10.0.0.0/25
    _allocate(client, dst, "req-held", 26)    # 10.0.0.0/26 inside that /25

    resp = _migrate(client, src, dst, "op-ovl", ["req-move"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "migration_allocation_overlap"

    # Neither pool changed.
    assert set(_allocated(client, src)) == {"req-move"}
    assert set(_allocated(client, dst)) == {"req-held"}

    # Once the blocking target allocation is released the move succeeds,
    # keeping the original CIDR.
    _release(client, dst, "req-held")
    ok = _migrate(client, src, dst, "op-ovl-ok", ["req-move"])
    assert ok.status_code == 201, ok.text
    assert ok.json()["migrated"][0]["cidr"] == "10.0.0.0/25"


def test_migration_failure_is_atomic(client):
    src = _create_pool(client, "10.0.0.0/24")
    dst = _create_pool(client, "10.0.0.0/16", exclusions=["10.0.0.192/26"])
    _allocate(client, src, "req-a", 26)  # 10.0.0.0/26: fine
    _allocate(client, src, "req-b", 26)  # 10.0.0.64/26: fine
    _allocate(client, src, "req-c", 26)  # 10.0.0.128/26: fine
    _allocate(client, src, "req-d", 26)  # 10.0.0.192/26: overlaps exclusion

    before_src = client.get(f"/pools/{src}").json()
    before_dst = client.get(f"/pools/{dst}").json()

    resp = _migrate(
        client, src, dst, "op-atomic", ["req-a", "req-b", "req-c", "req-d"]
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "migration_allocation_overlaps_exclusion"

    # Whole batch rolled back: no allocation from earlier in the manifest moved.
    assert client.get(f"/pools/{src}").json() == before_src
    assert client.get(f"/pools/{dst}").json() == before_dst


def test_concurrent_same_operation_id_migrates_once(client):
    src = _create_pool(client, "10.0.0.0/24")
    dst = _create_pool(client, "10.0.0.0/16")
    _allocate(client, src, "req-a", 26)
    bodies: list[dict] = []
    barrier = threading.Barrier(6)

    from app.main import app

    def worker():
        local = TestClient(app)
        barrier.wait()
        resp = _migrate(local, src, dst, "op-same", ["req-a"])
        assert resp.status_code in (200, 201), resp.text
        bodies.append(resp.json())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for b in bodies if not b["replayed"]) == 1
    assert {b["migration_id"] for b in bodies} == {bodies[0]["migration_id"]}
    assert set(_allocated(client, dst)) == {"req-a"}
    assert _allocated(client, src) == {}


def test_migration_visible_to_snapshot_after_move(client):
    src = _create_pool(client, "10.0.0.0/24")
    dst = _create_pool(client, "10.0.0.0/16")
    alloc = _allocate(client, src, "req-a", 26)

    assert _migrate(client, src, dst, "op-snap", ["req-a"]).status_code == 201

    snap = client.get(f"/pools/{dst}/snapshot")
    assert snap.status_code == 200
    doc = snap.json()["snapshot"]
    assert len(doc["allocations"]) == 1
    moved = doc["allocations"][0]
    assert moved["id"] == alloc["id"]
    assert moved["pool_id"] == dst
    assert moved["request_id"] == "req-a"
    assert moved["cidr"] == "10.0.0.0/26"
    assert client.get(f"/pools/{src}/snapshot").json()["snapshot"]["allocations"] == []
