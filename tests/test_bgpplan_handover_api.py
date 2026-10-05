"""API integration tests for BGP announcement plan handover."""

from __future__ import annotations

import json
import sqlite3
import threading

from fastapi.testclient import TestClient


RELS = [{"type": "customer_provider", "customer": 10, "provider": 100}]
ANN_25 = [{"id": "a0", "prefix": "203.0.113.0/25", "as_path": [10, 100]}]
ANN_24 = [{"id": "all", "prefix": "203.0.113.0/24", "as_path": [10, 100]}]


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
            "announcements": ANN_24 if announcements is None else announcements,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _activate(client, pool_id, plan_id):
    return client.post(f"/pools/{pool_id}/bgp-plans/{plan_id}/activate")


def _handover(client, pool_id, old_plan_ids, new_plan_id):
    return client.post(
        f"/pools/{pool_id}/bgp-plans/handover",
        json={"old_plan_ids": old_plan_ids, "new_plan_id": new_plan_id},
    )


def _plan_status(client, pool_id, plan_id, announcements=None, relationships=None):
    """Read a plan back through the idempotent create replay."""

    resp = client.post(
        f"/pools/{pool_id}/bgp-plans",
        json={
            "plan_id": plan_id,
            "relationships": RELS if relationships is None else relationships,
            "announcements": ANN_24 if announcements is None else announcements,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["replayed"] is True
    return resp.json()["status"]


def _seed_plan(plan_id, pool_id, status, params="{}", audit_results="[]",
               pool_state=None, state_digest="seed"):
    """Insert a plan row directly, bypassing the API (legacy data)."""

    from app import db as db_module

    if pool_state is None:
        pool_state = {"cidr": "203.0.113.0/24", "exclusions": [], "allocations": []}
    conn = sqlite3.connect(db_module.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO bgp_plans (plan_id, pool_id, params, audit_results, "
            "pool_state, state_digest, status, created_at, activated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                pool_id,
                params,
                audit_results,
                json.dumps(pool_state),
                state_digest,
                status,
                "2026-01-01T00:00:00+00:00",
                None if status == "planned" else "2026-01-02T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_handover_retires_active_and_activates_successor(client):
    pid = _create_pool(client)
    _allocate(client, pid, "order-1", 25)
    created_a = _create_plan(client, pid, "plan-a")
    assert _activate(client, pid, "plan-a").json()["status"] == "active"
    _create_plan(client, pid, "plan-b")

    resp = _handover(client, pid, ["plan-a"], "plan-b")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pool_id"] == pid
    # Request order: outgoing plans as listed, then the successor.
    assert [p["plan_id"] for p in body["plans"]] == ["plan-a", "plan-b"]
    retired, activated = body["plans"]
    assert retired["status"] == "retired"
    assert retired["activated_at"] is not None
    assert activated["status"] == "active"
    assert activated["activated_at"] is not None
    # The successor keeps its creation-time identity.
    assert activated["state_digest"] == created_a["state_digest"]

    # Stored statuses are visible through the idempotent create replay.
    assert _plan_status(client, pid, "plan-a") == "retired"
    assert _plan_status(client, pid, "plan-b") == "active"


def test_handover_response_follows_request_order_with_multiple_active(client):
    # Legacy databases may hold several active plans in one pool; the
    # handover retires all of them at once.
    pid = _create_pool(client)
    _allocate(client, pid, "order-1", 25)
    _seed_plan("legacy-x", pid, "active")
    _seed_plan("legacy-y", pid, "active")
    _create_plan(client, pid, "plan-b")

    resp = _handover(client, pid, ["legacy-y", "legacy-x"], "plan-b")
    assert resp.status_code == 200, resp.text
    plans = resp.json()["plans"]
    assert [p["plan_id"] for p in plans] == ["legacy-y", "legacy-x", "plan-b"]
    assert [p["status"] for p in plans] == ["retired", "retired", "active"]


def test_handover_list_must_equal_active_set(client):
    pid = _create_pool(client)
    _allocate(client, pid, "order-1", 25)
    _seed_plan("legacy-x", pid, "active")
    _seed_plan("legacy-y", pid, "active")
    _create_plan(client, pid, "plan-b")

    # Missing one active plan.
    resp = _handover(client, pid, ["legacy-x"], "plan-b")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_handover_set_mismatch"

    # The planned successor is not part of the active set.
    resp = _handover(client, pid, ["legacy-x", "legacy-y", "plan-b"], "plan-b")
    assert resp.status_code == 422  # successor listed among outgoing plans

    # A planned plan is not active either.
    resp = _handover(client, pid, ["legacy-x", "legacy-y", "plan-c"], "plan-b")
    assert resp.status_code == 404  # plan-c does not exist at all

    _create_plan(client, pid, "plan-c")
    resp = _handover(client, pid, ["legacy-x", "legacy-y", "plan-c"], "plan-b")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_handover_set_mismatch"

    # Nothing changed: both legacy plans still active, successors planned.
    from app import db as db_module

    conn = sqlite3.connect(db_module.DB_PATH)
    try:
        rows = dict(
            conn.execute("SELECT plan_id, status FROM bgp_plans").fetchall()
        )
    finally:
        conn.close()
    assert rows == {
        "legacy-x": "active",
        "legacy-y": "active",
        "plan-b": "planned",
        "plan-c": "planned",
    }


def test_handover_rejects_foreign_or_unknown_plans(client):
    p1 = _create_pool(client)
    p2 = _create_pool(client, cidr="203.0.114.0/24")
    _allocate(client, p1, "order-1", 25)
    _allocate(client, p2, "order-2", 25)
    _create_plan(client, p1, "plan-a")
    _activate(client, p1, "plan-a")
    _create_plan(
        client, p2, "plan-c",
        announcements=[{"id": "c", "prefix": "203.0.114.0/24", "as_path": [10, 100]}],
    )

    # Successor belongs to another pool.
    resp = _handover(client, p1, ["plan-a"], "plan-c")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "bgp_plan_not_found"

    # Outgoing plan belongs to another pool.
    _create_plan(client, p1, "plan-b")
    resp = _handover(client, p1, ["plan-c"], "plan-b")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "bgp_plan_not_found"

    # Unknown successor / unknown outgoing plan.
    assert _handover(client, p1, ["plan-a"], "nope").status_code == 404
    assert _handover(client, p1, ["nope"], "plan-b").status_code == 404

    # Unknown pool.
    resp = _handover(client, 999, ["plan-a"], "plan-b")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "pool_not_found"


def test_handover_rejects_non_planned_successor(client):
    pid = _create_pool(client)
    _allocate(client, pid, "order-1", 25)
    _create_plan(client, pid, "plan-a")
    _activate(client, pid, "plan-a")
    _create_plan(client, pid, "plan-b")

    # Successor already active: the current active plan cannot "take over".
    resp = _handover(client, pid, ["plan-a"], "plan-a")
    assert resp.status_code == 422  # successor listed among outgoing plans

    # Hand the pool over, then try to hand over to the retired plan.
    assert _handover(client, pid, ["plan-a"], "plan-b").status_code == 200
    resp = _handover(client, pid, ["plan-b"], "plan-a")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_not_planned"

    # Repeating the completed handover is a conflict, not a replay.
    resp = _handover(client, pid, ["plan-a"], "plan-b")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_not_planned"


def test_handover_rejects_stale_successor_state(client):
    pid = _create_pool(client)
    _allocate(client, pid, "order-1", 25)
    _create_plan(client, pid, "plan-a")
    _activate(client, pid, "plan-a")
    _create_plan(client, pid, "plan-b")

    # The pool grows an allocation plan-b does not cover: its pinned state
    # is stale, so the handover conflicts and nothing changes.
    _allocate(client, pid, "order-2", 25)
    resp = _handover(client, pid, ["plan-a"], "plan-b")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_state_conflict"
    assert _plan_status(client, pid, "plan-a") == "active"
    assert _plan_status(client, pid, "plan-b") == "planned"


def test_handover_request_shape_validation(client):
    pid = _create_pool(client)
    _allocate(client, pid, "order-1", 25)
    _create_plan(client, pid, "plan-a")
    _activate(client, pid, "plan-a")
    _create_plan(client, pid, "plan-b")

    # Empty outgoing list.
    resp = _handover(client, pid, [], "plan-b")
    assert resp.status_code == 422

    # Duplicate entries.
    resp = _handover(client, pid, ["plan-a", "plan-a"], "plan-b")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_handover_invalid"

    # Successor also listed as outgoing.
    resp = _handover(client, pid, ["plan-a", "plan-b"], "plan-b")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bgp_handover_invalid"


def test_single_activate_blocked_while_another_plan_is_active(client):
    pid = _create_pool(client)
    _allocate(client, pid, "order-1", 25)
    _create_plan(client, pid, "plan-a")
    _activate(client, pid, "plan-a")
    _create_plan(client, pid, "plan-b")

    resp = _activate(client, pid, "plan-b")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_handover_required"
    assert _plan_status(client, pid, "plan-b") == "planned"

    # Activating the already-active plan itself still replays fine.
    replay = _activate(client, pid, "plan-a")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True


def test_retired_plan_cannot_be_reactivated(client):
    pid = _create_pool(client)
    _allocate(client, pid, "order-1", 25)
    _create_plan(client, pid, "plan-a")
    _activate(client, pid, "plan-a")
    _create_plan(client, pid, "plan-b")
    assert _handover(client, pid, ["plan-a"], "plan-b").status_code == 200

    resp = _activate(client, pid, "plan-a")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_retired"
    assert _plan_status(client, pid, "plan-a") == "retired"


def test_create_replay_of_retired_plan_returns_identity(client):
    pid = _create_pool(client)
    _allocate(client, pid, "order-1", 25)
    created = _create_plan(client, pid, "plan-a")
    _activate(client, pid, "plan-a")
    _create_plan(client, pid, "plan-b")
    assert _handover(client, pid, ["plan-a"], "plan-b").status_code == 200

    # Replaying the create request with the same plan_id and parameters
    # returns the original plan with its current (retired) status.
    resp = client.post(
        f"/pools/{pid}/bgp-plans",
        json={"plan_id": "plan-a", "relationships": RELS, "announcements": ANN_24},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["replayed"] is True
    assert body["status"] == "retired"
    assert body["created_at"] == created["created_at"]
    assert body["state_digest"] == created["state_digest"]

    # Same plan_id with different parameters still conflicts.
    resp = client.post(
        f"/pools/{pid}/bgp-plans",
        json={"plan_id": "plan-a", "relationships": RELS, "announcements": ANN_25},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bgp_plan_id_conflict"


def test_handover_does_not_touch_other_pools(client):
    p1 = _create_pool(client)
    p2 = _create_pool(client, cidr="203.0.114.0/24")
    _allocate(client, p1, "order-1", 25)
    _allocate(client, p2, "order-2", 25)
    _create_plan(client, p1, "plan-a")
    _activate(client, p1, "plan-a")
    _create_plan(client, p1, "plan-b")
    ann_c = [{"id": "c", "prefix": "203.0.114.0/24", "as_path": [10, 100]}]
    _create_plan(client, p2, "plan-c", announcements=ann_c)
    _activate(client, p2, "plan-c")

    assert _handover(client, p1, ["plan-a"], "plan-b").status_code == 200
    assert _plan_status(client, p2, "plan-c", announcements=ann_c) == "active"


def test_concurrent_handover_only_one_commits(client):
    pid = _create_pool(client)
    _allocate(client, pid, "order-1", 25)
    _create_plan(client, pid, "plan-a")
    _activate(client, pid, "plan-a")
    _create_plan(client, pid, "plan-b")

    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, str]] = []
    lock = threading.Lock()

    from app.main import app

    def worker():
        local = TestClient(app)
        barrier.wait()
        resp = local.post(
            f"/pools/{pid}/bgp-plans/handover",
            json={"old_plan_ids": ["plan-a"], "new_plan_id": "plan-b"},
        )
        with lock:
            outcomes.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(code for code, _ in outcomes)
    assert statuses == [200, 409]
    # Final state is exactly one handover: plan-a retired, plan-b active.
    assert _plan_status(client, pid, "plan-a") == "retired"
    assert _plan_status(client, pid, "plan-b") == "active"


def test_retired_plan_does_not_block_renumber(client):
    # A pool whose only plans are retired (seeded legacy state) can be
    # renumbered: retired plans are history and pin nothing.
    pid = _create_pool(client)
    _allocate(client, pid, "order-1", 25)
    _seed_plan("legacy-retired", pid, "retired")

    resp = client.post(f"/pools/{pid}/renumber", json={"new_cidr": "203.0.113.0/24"})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Schema migration of pre-handover databases
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


def _build_legacy_db(db_path):
    """Create a database with the pre-handover schema and live plans."""

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE pools (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cidr        TEXT    NOT NULL UNIQUE,
                created_at  TEXT    NOT NULL
            );
            CREATE TABLE exclusions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_id     INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                cidr        TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                UNIQUE (pool_id, cidr)
            );
            CREATE TABLE allocations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_id         INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                cidr            TEXT    NOT NULL,
                request_id      TEXT    NOT NULL UNIQUE,
                prefix_length   INTEGER NOT NULL,
                created_at      TEXT    NOT NULL
            );
            """
            + _LEGACY_BGP_PLANS_DDL
        )
        conn.execute(
            "INSERT INTO pools (cidr, created_at) VALUES (?, ?)",
            ("203.0.113.0/24", "2026-01-01T00:00:00+00:00"),
        )
        pool_id = conn.execute("SELECT id FROM pools").fetchone()[0]

        from app.bgpplan import _canonical_params
        from app.schemas import BgpAnnouncement, CustomerProviderRelation

        params = json.dumps(
            _canonical_params(
                [CustomerProviderRelation(type="customer_provider", customer=10, provider=100)],
                [BgpAnnouncement(id="a", prefix="203.0.113.0/24", as_path=[10, 100])],
            )
        )
        pool_state = json.dumps(
            {"cidr": "203.0.113.0/24", "exclusions": [], "allocations": []}
        )
        # Two active plans in one pool (allowed by the old code) plus a
        # planned one; legacy-a carries a replayable parameter fingerprint.
        for plan_id, status in (
            ("legacy-a", "active"),
            ("legacy-b", "active"),
            ("legacy-c", "planned"),
        ):
            conn.execute(
                "INSERT INTO bgp_plans (plan_id, pool_id, params, audit_results, "
                "pool_state, state_digest, status, created_at, activated_at) "
                "VALUES (?, ?, ?, '[]', ?, 'legacy-digest', ?, ?, ?)",
                (
                    plan_id,
                    pool_id,
                    params if plan_id == "legacy-a" else "{}",
                    pool_state,
                    status,
                    "2026-01-01T00:00:00+00:00",
                    None if status == "planned" else "2026-01-02T00:00:00+00:00",
                ),
            )
        conn.commit()
        return pool_id
    finally:
        conn.close()


def test_legacy_database_is_migrated_and_usable(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    pool_id = _build_legacy_db(db_path)

    monkeypatch.setenv("SUBNET_DB_PATH", str(db_path))
    from app import db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    db_module.init_db()

    # Every legacy row survives with its plan_id and status; the pool index
    # is rebuilt on the new table.
    conn = sqlite3.connect(str(db_path))
    try:
        rows = dict(conn.execute("SELECT plan_id, status FROM bgp_plans").fetchall())
        assert rows == {
            "legacy-a": "active",
            "legacy-b": "active",
            "legacy-c": "planned",
        }
        indexes = {
            row[1]
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'bgp_plans'"
            )
        }
        assert "idx_bgp_plans_pool" in indexes
    finally:
        conn.close()

    from app.main import app

    with TestClient(app) as client:
        # The pool with two active plans keeps working: a single activation
        # of the planned legacy plan is refused, a handover retires both.
        _allocate(client, pool_id, "order-1", 25)
        _create_plan(client, pool_id, "plan-b")

        resp = _activate(client, pool_id, "legacy-c")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "bgp_plan_handover_required"

        resp = _handover(client, pool_id, ["legacy-a", "legacy-b"], "plan-b")
        assert resp.status_code == 200, resp.text
        assert [p["status"] for p in resp.json()["plans"]] == [
            "retired",
            "retired",
            "active",
        ]

        # plan_id identity survives the migration: replaying legacy-a's
        # create request returns the original plan, now retired.
        resp = client.post(
            f"/pools/{pool_id}/bgp-plans",
            json={
                "plan_id": "legacy-a",
                "relationships": RELS,
                "announcements": [
                    {"id": "a", "prefix": "203.0.113.0/24", "as_path": [10, 100]}
                ],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["replayed"] is True
        assert resp.json()["status"] == "retired"
        assert resp.json()["created_at"] == "2026-01-01T00:00:00+00:00"
