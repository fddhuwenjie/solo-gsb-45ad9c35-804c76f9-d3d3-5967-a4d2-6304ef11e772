"""Whole-pool renumbering: translate pool, exclusions and allocations together."""

from __future__ import annotations

import ipaddress
import sqlite3
import threading

from fastapi.testclient import TestClient

from app import db as db_module


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


def _renumber(client, pool_id, new_cidr):
    return client.post(f"/pools/{pool_id}/renumber", json={"new_cidr": new_cidr})


def _allocated(client, pool_id) -> dict[str, dict]:
    return {x["request_id"]: x for x in client.get(f"/pools/{pool_id}").json()["allocated"]}


def _raw_rows(pool_id):
    conn = sqlite3.connect(db_module.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        pool = conn.execute("SELECT * FROM pools WHERE id = ?", (pool_id,)).fetchone()
        exclusions = conn.execute(
            "SELECT * FROM exclusions WHERE pool_id = ? ORDER BY id", (pool_id,)
        ).fetchall()
        allocations = conn.execute(
            "SELECT * FROM allocations WHERE pool_id = ? ORDER BY id", (pool_id,)
        ).fetchall()
        return pool, exclusions, allocations
    finally:
        conn.close()


def test_renumber_translates_pool_exclusions_and_allocations(client):
    # 10.0.0.0/24 with an exclusion at offset .128/26 and two /26 allocations.
    created = client.post(
        "/pools",
        json={"cidr": "10.0.0.0/24", "exclusions": ["10.0.0.128/26"]},
    ).json()
    pool = created["id"]
    pool_created_at = created["created_at"]
    a = _allocate(client, pool, "req-a", 26)  # 10.0.0.0/26
    b = _allocate(client, pool, "req-b", 26)  # 10.0.0.64/26

    resp = _renumber(client, pool, "192.168.5.0/24")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pool_id"] == pool
    assert body["old_cidr"] == "10.0.0.0/24"
    assert body["new_cidr"] == "192.168.5.0/24"
    assert body["exclusions"] == [
        {"old_cidr": "10.0.0.128/26", "new_cidr": "192.168.5.128/26"}
    ]
    assert body["allocations"] == [
        {
            "allocation_id": a["id"],
            "request_id": "req-a",
            "old_cidr": "10.0.0.0/26",
            "new_cidr": "192.168.5.0/26",
            "prefix_length": 26,
        },
        {
            "allocation_id": b["id"],
            "request_id": "req-b",
            "old_cidr": "10.0.0.64/26",
            "new_cidr": "192.168.5.64/26",
            "prefix_length": 26,
        },
    ]

    # Pool status now reports the new addresses; the .128/26 exclusion is
    # preserved and only the .192/26 tail is free.
    status = client.get(f"/pools/{pool}").json()
    assert status["cidr"] == "192.168.5.0/24"
    assert status["exclusions"] == ["192.168.5.128/26"]
    assert status["free_cidrs"] == ["192.168.5.192/26"]
    current = _allocated(client, pool)
    assert current["req-a"]["cidr"] == "192.168.5.0/26"
    assert current["req-b"]["cidr"] == "192.168.5.64/26"

    # Row identity, request_id, prefix_length and created_at are preserved.
    db_pool, db_excl, db_alloc = _raw_rows(pool)
    assert db_pool["cidr"] == "192.168.5.0/24"
    assert db_pool["created_at"] == pool_created_at
    assert [r["cidr"] for r in db_excl] == ["192.168.5.128/26"]
    assert {r["id"] for r in db_alloc} == {a["id"], b["id"]}
    assert {r["request_id"] for r in db_alloc} == {"req-a", "req-b"}
    assert {r["prefix_length"] for r in db_alloc} == {26}
    assert {r["created_at"] for r in db_alloc} == {a["created_at"], b["created_at"]}

    # Idempotent allocation retries resolve to the new address.
    replay = client.post(
        f"/pools/{pool}/allocations",
        json={"request_id": "req-a", "prefix_length": 26},
    )
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    assert replay.json()["cidr"] == "192.168.5.0/26"
    assert replay.json()["id"] == a["id"]

    # Release also works against the new address.
    released = client.delete(f"/pools/{pool}/allocations/req-b")
    assert released.status_code == 200
    assert released.json()["released"] == "192.168.5.64/26"


def test_renumber_shift_is_offset_relative_to_pool_start(client):
    # A pool whose first address is not .0: 10.0.0.128/25.
    pool = _create_pool(client, "10.0.0.128/25")
    _allocate(client, pool, "r1", 26)  # 10.0.0.128/26 (offset 0)
    _allocate(client, pool, "r2", 27)  # 10.0.0.192/27 (offset 64)

    resp = _renumber(client, pool, "172.16.9.0/25")
    assert resp.status_code == 200, resp.text
    shifted = {a["request_id"]: a["new_cidr"] for a in resp.json()["allocations"]}
    assert shifted == {"r1": "172.16.9.0/26", "r2": "172.16.9.64/27"}


def test_renumber_downwards_in_address_space(client):
    pool = _create_pool(client, "192.168.5.0/24")
    _allocate(client, pool, "r1", 24)

    resp = _renumber(client, pool, "10.0.0.0/24")
    assert resp.status_code == 200, resp.text
    assert _allocated(client, pool)["r1"]["cidr"] == "10.0.0.0/24"


def test_renumber_empty_pool(client):
    pool = _create_pool(client, "10.0.0.0/24")
    resp = _renumber(client, pool, "10.1.0.0/24")
    assert resp.status_code == 200, resp.text
    assert resp.json()["exclusions"] == []
    assert resp.json()["allocations"] == []
    assert client.get(f"/pools/{pool}").json()["cidr"] == "10.1.0.0/24"
    # Allocation afterwards uses the new space.
    alloc = _allocate(client, pool, "after", 24)
    assert alloc["cidr"] == "10.1.0.0/24"


def test_renumber_same_cidr_is_a_noop(client):
    pool = _create_pool(client, "10.0.0.0/24", exclusions=["10.0.0.128/25"])
    alloc = _allocate(client, pool, "r1", 25)
    resp = _renumber(client, pool, "10.0.0.0/24")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["old_cidr"] == body["new_cidr"] == "10.0.0.0/24"
    assert body["exclusions"][0]["new_cidr"] == "10.0.0.128/25"
    assert body["allocations"][0]["new_cidr"] == "10.0.0.0/25"
    db_pool, _, db_alloc = _raw_rows(pool)
    assert db_pool["cidr"] == "10.0.0.0/24"
    assert db_alloc[0]["cidr"] == alloc["cidr"]


def test_renumber_unknown_pool_returns_404(client):
    resp = _renumber(client, 999, "10.1.0.0/24")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "pool_not_found"


def test_renumber_rejects_invalid_cidr(client):
    pool = _create_pool(client, "10.0.0.0/24")
    for bad in ["10.0.0.5/24", "not-a-cidr", "::1/128"]:
        resp = _renumber(client, pool, bad)
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["error"]["code"] == "invalid_cidr"
    # Pool unchanged.
    assert client.get(f"/pools/{pool}").json()["cidr"] == "10.0.0.0/24"


def test_renumber_rejects_changed_prefix_length(client):
    pool = _create_pool(client, "10.0.0.0/24")
    resp = _renumber(client, pool, "10.0.0.0/23")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "renumber_prefix_length_changed"
    # Pool unchanged.
    assert client.get(f"/pools/{pool}").json()["cidr"] == "10.0.0.0/24"


def test_renumber_rejects_overlap_with_other_pool(client):
    pool = _create_pool(client, "10.0.0.0/24", exclusions=["10.0.0.128/25"])
    _allocate(client, pool, "r1", 26)
    _create_pool(client, "10.1.0.0/24")

    resp = _renumber(client, pool, "10.1.0.0/24")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "renumber_pool_overlap"

    # The pool and all of its rows are untouched after the failure.
    status = client.get(f"/pools/{pool}").json()
    assert status["cidr"] == "10.0.0.0/24"
    assert status["exclusions"] == ["10.0.0.128/25"]
    assert _allocated(client, pool)["r1"]["cidr"] == "10.0.0.0/26"


def test_renumber_to_non_overlapping_addresses_when_pools_touch(client):
    first = _create_pool(client, "10.0.0.0/24")
    second = _create_pool(client, "10.0.1.0/24")
    _allocate(client, first, "f", 24)
    _allocate(client, second, "s", 24)
    # Adjacent (non-overlapping) ranges are fine in either direction.
    assert _renumber(client, first, "10.0.2.0/24").status_code == 200
    assert _allocated(client, first)["f"]["cidr"] == "10.0.2.0/24"
    # Moving back adjacent to the other pool also works.
    assert _renumber(client, first, "10.0.0.0/24").status_code == 200


def test_renumber_blocked_by_planned_bgp_plan(client):
    pool = _create_pool(client, "203.0.113.0/24")
    _allocate(client, pool, "r1", 25)
    plan = {
        "plan_id": "plan-1",
        "relationships": [
            {"type": "customer_provider", "customer": 10, "provider": 100}
        ],
        "announcements": [
            {"id": "a0", "prefix": "203.0.113.0/25", "as_path": [10, 100]},
            {"id": "a1", "prefix": "203.0.113.128/25", "as_path": [10, 100]},
        ],
    }
    resp = client.post(f"/pools/{pool}/bgp-plans", json=plan)
    assert resp.status_code == 201, resp.text

    blocked = _renumber(client, pool, "198.51.100.0/24")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "renumber_blocked_by_bgp_plan"

    # An active plan blocks too; deactivate is not offered, so test planned
    # only and verify the pool is untouched on refusal.
    assert client.get(f"/pools/{pool}").json()["cidr"] == "203.0.113.0/24"


def test_renumber_blocked_by_active_bgp_plan(client):
    pool = _create_pool(client, "203.0.113.0/24")
    _allocate(client, pool, "r1", 25)
    client.post(
        f"/pools/{pool}/bgp-plans",
        json={
            "plan_id": "plan-1",
            "relationships": [
                {"type": "customer_provider", "customer": 10, "provider": 100}
            ],
            "announcements": [
                {"id": "a0", "prefix": "203.0.113.0/25", "as_path": [10, 100]},
                {"id": "a1", "prefix": "203.0.113.128/25", "as_path": [10, 100]},
            ],
        },
    )
    activated = client.post(f"/pools/{pool}/bgp-plans/plan-1/activate")
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"

    blocked = _renumber(client, pool, "198.51.100.0/24")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "renumber_blocked_by_bgp_plan"
    assert client.get(f"/pools/{pool}").json()["cidr"] == "203.0.113.0/24"


def test_renumber_blocked_by_completed_reservation_batch(client):
    pool = _create_pool(client, "10.0.0.0/24")
    resp = client.post(
        f"/pools/{pool}/reservations",
        json={
            "batch_id": "batch-1",
            "default_action": "allow",
            "rules": [],
            "items": [{"request_id": "r1", "prefix_length": 26}],
        },
    )
    assert resp.status_code == 201, resp.text

    blocked = _renumber(client, pool, "10.1.0.0/24")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "renumber_blocked_by_reservation_batch"
    # The batch replay still returns the original (old) address after refusal.
    replay = client.post(
        f"/pools/{pool}/reservations",
        json={
            "batch_id": "batch-1",
            "default_action": "allow",
            "rules": [],
            "items": [{"request_id": "r1", "prefix_length": 26}],
        },
    )
    assert replay.status_code == 200
    assert replay.json()["reservations"][0]["cidr"] == "10.0.0.0/26"


def test_renumber_blocked_when_pool_is_migration_source(client):
    source = _create_pool(client, "10.0.0.0/24")
    target = _create_pool(client, "10.0.0.0/16")
    _allocate(client, source, "m1", 26)
    resp = client.post(
        f"/pools/{source}/allocation-migrations",
        json={
            "operation_id": "mig-1",
            "target_pool_id": target,
            "allocations": [{"request_id": "m1"}],
        },
    )
    assert resp.status_code == 201, resp.text

    blocked = _renumber(client, source, "10.1.0.0/24")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "renumber_blocked_by_allocation_migration"
    assert client.get(f"/pools/{source}").json()["cidr"] == "10.0.0.0/24"


def test_renumber_blocked_when_pool_is_migration_target(client):
    source = _create_pool(client, "10.0.0.0/24")
    target = _create_pool(client, "10.0.0.0/16")
    _allocate(client, source, "m1", 26)
    client.post(
        f"/pools/{source}/allocation-migrations",
        json={
            "operation_id": "mig-1",
            "target_pool_id": target,
            "allocations": [{"request_id": "m1"}],
        },
    )

    blocked = _renumber(client, target, "10.2.0.0/16")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "renumber_blocked_by_allocation_migration"
    assert client.get(f"/pools/{target}").json()["cidr"] == "10.0.0.0/16"


def test_renumber_failure_leaves_everything_unchanged(client):
    pool = _create_pool(client, "10.0.0.0/24", exclusions=["10.0.0.64/26"])
    alloc = _allocate(client, pool, "r1", 26)
    _create_pool(client, "10.1.0.0/24")
    before_pool, before_excl, before_alloc = _raw_rows(pool)

    resp = _renumber(client, pool, "10.1.0.0/24")
    assert resp.status_code == 409

    after_pool, after_excl, after_alloc = _raw_rows(pool)
    assert dict(after_pool) == dict(before_pool)
    assert [dict(r) for r in after_excl] == [dict(r) for r in before_excl]
    assert [dict(r) for r in after_alloc] == [dict(r) for r in before_alloc]
    assert after_alloc[0]["cidr"] == alloc["cidr"]


def test_renumber_preserves_free_space_shape(client):
    # /24 with two /26 allocations and a /26 exclusion: the translated free
    # set must be the exact translate of the old free set.
    pool = _create_pool(client, "10.0.0.0/24", exclusions=["10.0.0.128/26"])
    _allocate(client, pool, "r1", 26)  # .0/26
    _allocate(client, pool, "r2", 26)  # .64/26
    before = client.get(f"/pools/{pool}").json()["free_cidrs"]

    assert _renumber(client, pool, "198.51.100.0/24").status_code == 200
    after = client.get(f"/pools/{pool}").json()["free_cidrs"]

    def shift(cidr: str) -> str:
        net = ipaddress.IPv4Network(cidr)
        offset = int(net.network_address) - int(ipaddress.IPv4Address("10.0.0.0"))
        new_start = int(ipaddress.IPv4Address("198.51.100.0"))
        return str(ipaddress.IPv4Network((new_start + offset, net.prefixlen)))

    assert sorted(after) == sorted(shift(c) for c in before)


def test_other_pool_renumber_is_independent(client):
    p1 = _create_pool(client, "10.0.0.0/24")
    p2 = _create_pool(client, "10.1.0.0/24")
    _allocate(client, p1, "a", 24)
    _allocate(client, p2, "b", 24)
    assert _renumber(client, p1, "10.2.0.0/24").status_code == 200
    assert _allocated(client, p1)["a"]["cidr"] == "10.2.0.0/24"
    assert _allocated(client, p2)["b"]["cidr"] == "10.1.0.0/24"


def test_concurrent_renumbers_to_overlapping_targets_serialize(client):
    # Two disjoint pools raced onto the same /24: the write transactions
    # serialise, so exactly one wins the address and the loser is rejected
    # with renumber_pool_overlap instead of two overlapping pools existing.
    p1 = _create_pool(client, "10.0.0.0/24")
    p2 = _create_pool(client, "10.1.0.0/24")
    _allocate(client, p1, "a", 30)
    _allocate(client, p2, "b", 30)
    results: list[tuple[int, str]] = []
    barrier = threading.Barrier(2)

    from app.main import app

    def worker(pool_id, new_cidr):
        local = TestClient(app)
        barrier.wait()
        resp = local.post(f"/pools/{pool_id}/renumber", json={"new_cidr": new_cidr})
        results.append((resp.status_code, resp.json()["error"]["code"] if resp.status_code >= 400 else "ok"))

    threads = [
        threading.Thread(target=worker, args=(p1, "10.9.9.0/24")),
        threading.Thread(target=worker, args=(p2, "10.9.9.0/24")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(code for _, code in results) == ["ok", "renumber_pool_overlap"], results
    assert sorted(status for status, _ in results) == [200, 409]

    # Exactly one pool owns the target; the rejected pool kept its old range.
    cidrs = {client.get(f"/pools/{p}").json()["cidr"] for p in (p1, p2)}
    assert cidrs == {"10.9.9.0/24", "10.0.0.0/24"} or cidrs == {
        "10.9.9.0/24",
        "10.1.0.0/24",
    }
