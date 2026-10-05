"""Confirmable BGP announcement plans bound to a pool's state.

``POST /pools/{id}/bgp-plans`` records an immutable announcement plan,
``POST /pools/{id}/bgp-plans/{plan_id}/activate`` marks it active, and
``POST /pools/{id}/bgp-plans/handover`` atomically retires every currently
active plan of the pool while activating one stored ``planned`` successor.
A plan is only accepted when every check below passes in one go:

* the plan's announcements are audited with the same stateless path audit as
  ``/bgp/announcements/audit`` (relationship/path-level failures still 422);
* every announcement prefix is fully inside the pool and does not overlap any
  exclusion CIDR;
* every current allocation is covered by at least one announcement whose
  audit status is ``valid``;
* no allocation is covered by announcements originated by different ASes.

The pool's exclusions and allocations are read in one consistent read
transaction and pinned into the stored plan as a canonical
:class:`~app.schemas.BgpPlanPoolState` summary plus its SHA-256 digest.
Activation re-reads the pool inside one write transaction: the digest must
still match and the coverage/origins relationship must still hold, otherwise
the request conflicts and nothing is partially applied.

Plan status moves ``planned`` -> ``active`` -> ``retired``; ``retired`` is
terminal.  A single activation is refused while another plan of the pool is
active -- replacing active plans is what the handover endpoint is for.  The
handover runs entirely inside one ``BEGIN IMMEDIATE`` write transaction: the
submitted list of outgoing plans must equal the pool's current active set
exactly, every named plan must belong to the pool, and the successor's
pinned digest plus its stored audit results are re-verified against the
current pool state before any status flips.  Any failure (stale state,
wrong pool, a concurrent handover that committed first) rolls everything
back, so plan statuses never change partially.

``plan_id`` is the idempotency key: replaying the same plan with the same
parameters returns the stored plan (including a ``retired`` status once a
handover has retired it); changing the parameters (relationships or
announcements, order-insensitive) is a 409 conflict.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import sqlite3

from .bgpaudit import BgpAuditError, audit
from .cidrmath import parse_network
from .db import read_transaction, write_transaction
from .schemas import SNAPSHOT_VERSION
from .service import (
    ApiError,
    _pool_allocations,
    _pool_exclusions,
    _row_pool,
    utc_now,
)


def _plan_error(detail: str, code: str) -> ApiError:
    return ApiError(422, detail, code=code)


def _canonical_params(relationships, announcements) -> dict:
    """Build the order-insensitive parameter fingerprint of a plan request.

    Relationships are deduplicated and sorted (the audit itself collapses
    identical repeats); announcements are sorted by id and carry their
    normalised prefix and the submitted path.  Two requests with the same
    fingerprint are replays of one another.
    """

    relation_items = sorted(
        {
            (
                ("c2p", rel.customer, rel.provider)
                if rel.type == "customer_provider"
                else ("peer", min(rel.a, rel.b), max(rel.a, rel.b))
            )
            for rel in relationships
        }
    )
    announcement_items = sorted(
        (ann.id, str(parse_network(ann.prefix)), list(ann.as_path))
        for ann in announcements
    )
    return {"relationships": relation_items, "announcements": announcement_items}


def _pool_state(conn: sqlite3.Connection, pool_id: int) -> dict:
    """Snapshot the pool's CIDR, exclusions and allocations in canonical order."""

    pool = _row_pool(conn, pool_id)
    exclusions = sorted(
        (parse_network(row["cidr"]) for row in _pool_exclusions(conn, pool_id)),
        key=lambda net: int(net.network_address),
    )
    allocation_rows = _pool_allocations(conn, pool_id)
    allocation_rows = sorted(
        allocation_rows,
        key=lambda row: int(parse_network(row["cidr"]).network_address),
    )
    return {
        "cidr": pool["cidr"],
        "exclusions": [str(net) for net in exclusions],
        "allocations": [
            {
                "cidr": row["cidr"],
                "request_id": row["request_id"],
                "prefix_length": row["prefix_length"],
                "created_at": row["created_at"],
            }
            for row in allocation_rows
        ],
    }


def _state_digest(pool_state: dict) -> str:
    """SHA-256 over the canonical JSON of the full pinned pool-state summary.

    The summary covers the pool CIDR, every exclusion and every allocation
    (including its ``request_id``, ``prefix_length`` and ``created_at``), so
    any change to the pool's rows -- even a release re-allocated with the
    same request id at the same CIDR -- produces a different digest.
    """

    identity = {
        "version": SNAPSHOT_VERSION,
        "cidr": pool_state["cidr"],
        "exclusions": list(pool_state["exclusions"]),
        "allocations": [
            {
                "cidr": item["cidr"],
                "request_id": item["request_id"],
                "prefix_length": item["prefix_length"],
                "created_at": item["created_at"],
            }
            for item in pool_state["allocations"]
        ],
    }
    canonical = json.dumps(
        identity, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_coverage(
    results: list[dict],
    pool_net: ipaddress.IPv4Network,
    exclusions: list[ipaddress.IPv4Network],
    allocation_rows: list[sqlite3.Row],
) -> None:
    """Run all announcement-vs-pool checks; raise on the first failure.

    Every announcement prefix must lie fully inside the pool and avoid every
    exclusion.  Only announcements whose audited status is ``valid`` may
    cover allocations; each allocation needs at least one such covering
    announcement, and all announcements covering one allocation must share a
    single origin.
    """

    valid: list[dict] = []
    for result in results:
        net = parse_network(result["prefix"])
        if not net.subnet_of(pool_net):
            raise _plan_error(
                f"announcement {result['id']!r} prefix {net} is not inside "
                f"pool {pool_net}",
                code="bgp_announcement_outside_pool",
            )
        if any(net.overlaps(excluded) for excluded in exclusions):
            raise _plan_error(
                f"announcement {result['id']!r} prefix {net} overlaps a pool "
                "exclusion range",
                code="bgp_announcement_overlaps_exclusion",
            )
        if result["status"] == "valid":
            valid.append(result)

    for row in allocation_rows:
        allocated = parse_network(row["cidr"])
        covering = [r for r in valid if allocated.subnet_of(parse_network(r["prefix"]))]
        if not covering:
            raise _plan_error(
                f"allocation {allocated} (request_id {row['request_id']!r}) is not "
                "covered by any announcement that passed the path audit",
                code="bgp_allocation_uncovered",
            )
        origins = {r["origin"] for r in covering}
        if len(origins) > 1:
            ids = ", ".join(sorted(r["id"] for r in covering))
            raise _plan_error(
                f"allocation {allocated} is covered by announcements with different "
                f"origins {sorted(origins)} ({ids})",
                code="bgp_allocation_origin_conflict",
            )


def _verify_plan_fresh(conn: sqlite3.Connection, pool_id: int, row: sqlite3.Row) -> None:
    """Re-verify a stored plan against the pool's current state.

    The pinned pool-state digest must still match and the stored audited
    announcements must still cover every current allocation with a single
    origin; any drift is a 409 conflict and nothing is applied.
    """

    pool = _row_pool(conn, pool_id)
    pool_net = parse_network(pool["cidr"])
    exclusions = [
        parse_network(r["cidr"]) for r in _pool_exclusions(conn, pool_id)
    ]
    allocation_rows = _pool_allocations(conn, pool_id)
    current_state = _pool_state(conn, pool_id)

    if _state_digest(current_state) != row["state_digest"]:
        raise ApiError(
            409,
            f"pool {pool_id} state no longer matches the summary pinned by "
            f"plan {row['plan_id']!r}; refuse to activate stale announcements",
            code="bgp_plan_state_conflict",
        )

    # Redundant by the digest check, but required explicitly: re-prove
    # coverage and single-origin with the stored audited announcements.
    try:
        _validate_coverage(
            json.loads(row["audit_results"]),
            pool_net,
            exclusions,
            allocation_rows,
        )
    except ApiError:
        raise ApiError(
            409,
            f"plan {row['plan_id']!r} coverage no longer holds against pool "
            f"{pool_id}; refuse to activate",
            code="bgp_plan_state_conflict",
        )


def _response(row: sqlite3.Row, replayed: bool) -> dict:
    return {
        "plan_id": row["plan_id"],
        "pool_id": row["pool_id"],
        "status": row["status"],
        "results": json.loads(row["audit_results"]),
        "state_digest": row["state_digest"],
        "pool_state": json.loads(row["pool_state"]),
        "created_at": row["created_at"],
        "activated_at": row["activated_at"],
        "replayed": replayed,
    }


def create_bgp_plan(pool_id: int, plan_id: str, relationships, announcements) -> dict:
    """Validate, pin and store an immutable BGP announcement plan."""

    # Run the existing path audit first: relationship conflicts, unknown
    # relation edges, bad prefixes, empty paths and duplicate ids fail before
    # the database is touched and surface as 422 just like the audit API.
    try:
        results = audit(relationships, announcements)["results"]
    except BgpAuditError as exc:
        raise ApiError(422, exc.detail, code=exc.code)

    params_json = json.dumps(_canonical_params(relationships, announcements))

    # Fix the pool's exclusions and allocations in one consistent read
    # transaction; every containment/coverage check below uses this snapshot.
    with read_transaction() as conn:
        existing = conn.execute(
            "SELECT * FROM bgp_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if existing is not None:
            if existing["pool_id"] != pool_id or existing["params"] != params_json:
                raise ApiError(
                    409,
                    f"plan_id {plan_id!r} was already used with different parameters",
                    code="bgp_plan_id_conflict",
                )
            return _response(existing, replayed=True)

        pool = _row_pool(conn, pool_id)
        pool_net = parse_network(pool["cidr"])
        exclusions = [
            parse_network(row["cidr"]) for row in _pool_exclusions(conn, pool_id)
        ]
        allocation_rows = _pool_allocations(conn, pool_id)
        pool_state = _pool_state(conn, pool_id)

    _validate_coverage(results, pool_net, exclusions, allocation_rows)
    state_digest = _state_digest(pool_state)

    now = utc_now()
    with write_transaction() as conn:
        # Authoritative replay/conflict check: a concurrent request with the
        # same plan_id may have committed while validation ran.
        existing = conn.execute(
            "SELECT * FROM bgp_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if existing is not None:
            if existing["pool_id"] != pool_id or existing["params"] != params_json:
                raise ApiError(
                    409,
                    f"plan_id {plan_id!r} was already used with different parameters",
                    code="bgp_plan_id_conflict",
                )
            return _response(existing, replayed=True)

        # Re-read state under the write lock: a plan may only be pinned when
        # the checks and the stored summary describe the same pool state.
        current_state = _pool_state(conn, pool_id)
        if _state_digest(current_state) != state_digest:
            raise ApiError(
                409,
                f"pool {pool_id} changed while plan {plan_id!r} was being prepared",
                code="bgp_plan_state_conflict",
            )

        try:
            cursor = conn.execute(
                "INSERT INTO bgp_plans "
                "(plan_id, pool_id, params, audit_results, pool_state, state_digest, "
                "status, created_at, activated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, NULL)",
                (
                    plan_id,
                    pool_id,
                    params_json,
                    json.dumps(results),
                    json.dumps(pool_state),
                    state_digest,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise ApiError(
                409,
                f"plan_id {plan_id!r} conflicts with an existing plan",
                code="bgp_plan_id_conflict",
            )
        row = conn.execute(
            "SELECT * FROM bgp_plans WHERE rowid = ?",
            (cursor.lastrowid,),
        ).fetchone()

    return _response(row, replayed=False)


def activate_bgp_plan(pool_id: int, plan_id: str) -> dict:
    """Mark a stored plan active after re-verifying it against current state."""

    with write_transaction() as conn:
        row = conn.execute(
            "SELECT * FROM bgp_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if row is None:
            raise ApiError(
                404,
                f"BGP plan {plan_id!r} not found",
                code="bgp_plan_not_found",
            )
        if row["pool_id"] != pool_id:
            raise ApiError(
                404,
                f"BGP plan {plan_id!r} does not belong to pool {pool_id}",
                code="bgp_plan_not_found",
            )

        # Activation is idempotent: an already active plan replays its result
        # without re-checking pool state.
        if row["status"] == "active":
            return _response(row, replayed=True)

        # Retirement is terminal: a plan replaced by a handover never comes
        # back; create a new plan (new plan_id) to announce again.
        if row["status"] == "retired":
            raise ApiError(
                409,
                f"BGP plan {plan_id!r} was retired by a handover and cannot "
                "be re-activated",
                code="bgp_plan_retired",
            )

        # A pool carries at most one active plan: while another plan is
        # active, a planned one can only take over through the handover
        # endpoint, which retires the outgoing plans atomically.
        other = conn.execute(
            "SELECT plan_id FROM bgp_plans "
            "WHERE pool_id = ? AND status = 'active' LIMIT 1",
            (pool_id,),
        ).fetchone()
        if other is not None:
            raise ApiError(
                409,
                f"pool {pool_id} already has active BGP plan "
                f"{other['plan_id']!r}; use the handover endpoint to retire "
                "it and activate a successor atomically",
                code="bgp_plan_handover_required",
            )

        _verify_plan_fresh(conn, pool_id, row)

        now = utc_now()
        conn.execute(
            "UPDATE bgp_plans SET status = 'active', activated_at = ? "
            "WHERE plan_id = ? AND status = 'planned'",
            (now, plan_id),
        )
        row = conn.execute(
            "SELECT * FROM bgp_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()

    return _response(row, replayed=False)


def handover_bgp_plans(pool_id: int, old_plan_ids: list[str], new_plan_id: str) -> dict:
    """Atomically retire every active plan of a pool and activate a successor.

    The whole check-and-flip runs inside one ``BEGIN IMMEDIATE`` write
    transaction: the request must name every currently active plan of the
    pool (at least one), every named plan must belong to the pool, and the
    successor must be a stored ``planned`` plan whose pinned pool state
    still matches.  Any failure -- stale state, wrong pool, or a concurrent
    handover that committed first -- rolls the transaction back, so no plan
    status changes partially.  A handover is not replayable: once it
    commits, repeating the same request conflicts because the outgoing
    plans are retired and the successor is active.
    """

    # Cheap request-shape checks before any transaction: the outgoing list
    # must be duplicate-free and must not contain the successor.
    if len(set(old_plan_ids)) != len(old_plan_ids):
        raise ApiError(
            422,
            "old_plan_ids contains duplicate plan ids",
            code="bgp_handover_invalid",
        )
    if new_plan_id in old_plan_ids:
        raise ApiError(
            422,
            f"successor plan {new_plan_id!r} must not also appear in old_plan_ids",
            code="bgp_handover_invalid",
        )

    with write_transaction() as conn:
        _row_pool(conn, pool_id)

        def _plan_row(plan_id: str) -> sqlite3.Row:
            row = conn.execute(
                "SELECT * FROM bgp_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None or row["pool_id"] != pool_id:
                raise ApiError(
                    404,
                    f"BGP plan {plan_id!r} not found in pool {pool_id}",
                    code="bgp_plan_not_found",
                )
            return row

        # The successor must be a stored, still-planned plan of this pool.
        successor = _plan_row(new_plan_id)
        if successor["status"] != "planned":
            raise ApiError(
                409,
                f"successor plan {new_plan_id!r} is {successor['status']}, not "
                "planned; a handover activates a planned plan exactly once",
                code="bgp_plan_not_planned",
            )

        # Every outgoing plan must exist and belong to this pool, and the
        # submitted list must equal the pool's current active set exactly --
        # no missing, extra or non-active entries.
        for plan_id in old_plan_ids:
            _plan_row(plan_id)
        active_ids = {
            row["plan_id"]
            for row in conn.execute(
                "SELECT plan_id FROM bgp_plans "
                "WHERE pool_id = ? AND status = 'active'",
                (pool_id,),
            ).fetchall()
        }
        if set(old_plan_ids) != active_ids:
            raise ApiError(
                409,
                f"old_plan_ids {sorted(old_plan_ids)} do not match the active "
                f"BGP plans of pool {pool_id} ({sorted(active_ids)}); the "
                "handover list must name every currently active plan",
                code="bgp_handover_set_mismatch",
            )

        # The successor's pinned pool state must still be current and its
        # stored audit results must still cover every allocation with a
        # single origin.
        _verify_plan_fresh(conn, pool_id, successor)

        # All checks passed: retire the outgoing plans and activate the
        # successor in the same transaction.  The status guards plus
        # rowcount assertions are defence in depth; BEGIN IMMEDIATE already
        # serialises writers, so a concurrent handover is observed at the
        # checks above and conflicts there.
        now = utc_now()
        for plan_id in old_plan_ids:
            cursor = conn.execute(
                "UPDATE bgp_plans SET status = 'retired' "
                "WHERE plan_id = ? AND status = 'active'",
                (plan_id,),
            )
            if cursor.rowcount != 1:
                raise ApiError(
                    409,
                    f"BGP plan {plan_id!r} is no longer active; a concurrent "
                    "handover changed the pool's active set",
                    code="bgp_handover_set_mismatch",
                )
        cursor = conn.execute(
            "UPDATE bgp_plans SET status = 'active', activated_at = ? "
            "WHERE plan_id = ? AND status = 'planned'",
            (now, new_plan_id),
        )
        if cursor.rowcount != 1:
            raise ApiError(
                409,
                f"successor plan {new_plan_id!r} is no longer planned; a "
                "concurrent handover changed it first",
                code="bgp_plan_not_planned",
            )

        # Return every touched plan in request order: the outgoing plans as
        # listed, then the activated successor.
        plans = [
            _response(
                conn.execute(
                    "SELECT * FROM bgp_plans WHERE plan_id = ?",
                    (plan_id,),
                ).fetchone(),
                replayed=False,
            )
            for plan_id in [*old_plan_ids, new_plan_id]
        ]

    return {"pool_id": pool_id, "plans": plans}
