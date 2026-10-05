"""Safe decommissioning of an allocated subnet together with a BGP plan cutover.

``POST /pools/{id}/decommission`` retires one still-active allocation while
the pool keeps other allocations in service.  The caller submits:

* ``request_id`` -- the allocation to release;
* ``active_plan_id`` -- the pool's single currently active BGP plan, which
  must still describe the pool exactly as it is before the release;
* ``new_plan_id`` -- the idempotency key of the successor plan created by
  this operation;
* ``relationships`` -- AS relationships used by the same stateless path
  audit as ``/bgp/announcements/audit`` and ``/bgp-plans``;
* ``announcements`` -- the successor announcements covering every
  *remaining* allocation after the target is released.

Everything happens inside one ``BEGIN IMMEDIATE`` write transaction.  The
service first confirms the pool has exactly the one named active plan and
that the plan's pinned pool-state digest (plus its stored audit results)
still matches the current state, i.e. the fixed pool state is consistent
with what the caller believes to be current.  Only then is the target
allocation deleted.  The successor announcements are audited with the
existing path audit and checked against the post-release pool with the
existing plan coverage rules; in addition they must not intersect the
released CIDR, must cover every remaining allocation and must keep a
single origin per allocation.  When every check passes, the successor
plan is stored pinned to the post-release pool state, the outgoing plan
is retired and the successor activated, all in the same commit.

A plan id clash, a stale pool state, an unknown/wrong-pool plan or
allocation, or any announcement/coverage failure rolls the transaction
back: neither the allocation nor any plan status changes partially.

The operation is intentionally not idempotent (like the handover): once it
has committed, the target allocation is gone and the successor plan is
active, so repeating the request conflicts.  Provision history is
unaffected -- ``address_provisions`` rows only reference allocations
historically, and replaying a provision whose allocation was released
here still returns the stored result without re-occupying any address.
"""

from __future__ import annotations

import json
import sqlite3

from .bgpaudit import BgpAuditError, audit
from .bgpplan import (
    _canonical_params,
    _plan_error,
    _pool_state,
    _response as _plan_response,
    _state_digest,
    _validate_coverage,
    _verify_plan_fresh,
)
from .cidrmath import parse_network
from .db import write_transaction
from .service import (
    ApiError,
    _pool_allocations,
    _pool_exclusions,
    _row_pool,
    utc_now,
)


def _validate_successor(
    results: list[dict],
    pool_net,
    exclusions,
    released_net,
    remaining_rows: list[sqlite3.Row],
) -> None:
    """Run the successor-announcement checks against the post-release state.

    On top of the existing plan coverage rules (every announcement inside
    the pool and clear of exclusions, every remaining allocation covered by
    a ``valid`` announcement, a single origin per allocation), every
    successor announcement -- including ones the path audit marks
    ``as_loop``/``invalid_path`` -- must avoid the released CIDR entirely,
    so the decommissioned range stops being announced.
    """

    for result in results:
        net = parse_network(result["prefix"])
        if net.overlaps(released_net):
            raise _plan_error(
                f"successor announcement {result['id']!r} prefix {net} intersects "
                f"the released CIDR {released_net}; the decommissioned range must "
                "not be announced",
                code="bgp_announcement_overlaps_released",
            )

    _validate_coverage(results, pool_net, exclusions, remaining_rows)


def decommission(
    pool_id: int,
    request_id: str,
    active_plan_id: str,
    new_plan_id: str,
    relationships,
    announcements,
) -> dict:
    """Release one allocation and cut the pool's BGP plan over atomically."""

    if active_plan_id == new_plan_id:
        raise ApiError(
            422,
            f"new_plan_id {new_plan_id!r} must differ from active_plan_id",
            code="bgp_decommission_invalid",
        )

    # Run the existing stateless path audit before opening the transaction,
    # exactly like plan creation/provisioning: relationship conflicts,
    # unknown relation edges, bad prefixes, empty paths and duplicate ids
    # surface as 422 without touching the database.
    try:
        results = audit(relationships, announcements)["results"]
    except BgpAuditError as exc:
        raise ApiError(422, exc.detail, code=exc.code)

    params_json = json.dumps(_canonical_params(relationships, announcements))

    # All checks and writes -- active-plan pinning, allocation release,
    # successor audit/coverage validation, successor insert and status
    # flips -- happen in one immediate write transaction.  Any failure
    # rolls everything back together.
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

        # The named active plan must exist and belong to this pool before
        # the pool's active set is compared with it.
        active_row = _plan_row(active_plan_id)

        # The pool must carry exactly the one named active plan: a retired
        # or planned plan cannot be decommissioned from, and legacy pools
        # with several active plans must use the regular handover endpoint.
        active_rows = conn.execute(
            "SELECT plan_id FROM bgp_plans WHERE pool_id = ? AND status = 'active'",
            (pool_id,),
        ).fetchall()
        active_ids = {row["plan_id"] for row in active_rows}
        if active_ids != {active_plan_id}:
            raise ApiError(
                409,
                f"active_plan_id {active_plan_id!r} does not match the active "
                f"BGP plans of pool {pool_id} ({sorted(active_ids)}); the pool "
                "must have exactly this one active plan",
                code="bgp_handover_set_mismatch",
            )

        # A successor id already in use (standalone plan or another
        # provision/decommission) cannot be taken over; the bgp_plans
        # primary key additionally guards concurrent writers.
        if conn.execute(
            "SELECT 1 FROM bgp_plans WHERE plan_id = ?",
            (new_plan_id,),
        ).fetchone() is not None:
            raise ApiError(
                409,
                f"plan_id {new_plan_id!r} already exists; it cannot be reused "
                "as the successor plan",
                code="bgp_plan_id_conflict",
            )

        target = conn.execute(
            "SELECT * FROM allocations WHERE pool_id = ? AND request_id = ?",
            (pool_id, request_id),
        ).fetchone()
        if target is None:
            raise ApiError(
                404,
                f"request_id {request_id!r} not found in pool {pool_id}",
                code="allocation_not_found",
            )
        released_net = parse_network(target["cidr"])

        remaining_before = _pool_allocations(conn, pool_id)
        if len(remaining_before) <= 1:
            raise ApiError(
                422,
                f"allocation {released_net} is the only allocation in pool "
                f"{pool_id}; safe decommission requires other allocations to "
                "remain in service",
                code="decommission_no_remaining_allocations",
            )

        # Fix the pool state as the caller sees it: the active plan's
        # pinned summary must still match and its stored audit results must
        # still cover every current allocation with a single origin.
        _verify_plan_fresh(conn, pool_id, active_row)

        pool = _row_pool(conn, pool_id)
        pool_net = parse_network(pool["cidr"])
        exclusions = [parse_network(r["cidr"]) for r in _pool_exclusions(conn, pool_id)]

        # Release the target allocation, then validate the successor
        # announcements against exactly the post-release pool state.
        conn.execute("DELETE FROM allocations WHERE id = ?", (target["id"],))
        remaining_rows = _pool_allocations(conn, pool_id)
        _validate_successor(
            results, pool_net, exclusions, released_net, remaining_rows
        )

        post_state = _pool_state(conn, pool_id)
        state_digest = _state_digest(post_state)
        now = utc_now()

        try:
            successor_cursor = conn.execute(
                "INSERT INTO bgp_plans "
                "(plan_id, pool_id, params, audit_results, pool_state, state_digest, "
                "status, created_at, activated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    new_plan_id,
                    pool_id,
                    params_json,
                    json.dumps(results),
                    json.dumps(post_state),
                    state_digest,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise ApiError(
                409,
                f"plan_id {new_plan_id!r} conflicts with an existing BGP plan",
                code="bgp_plan_id_conflict",
            )

        cursor = conn.execute(
            "UPDATE bgp_plans SET status = 'retired' "
            "WHERE plan_id = ? AND status = 'active'",
            (active_plan_id,),
        )
        if cursor.rowcount != 1:
            raise ApiError(
                409,
                f"BGP plan {active_plan_id!r} is no longer active; a concurrent "
                "operation changed the pool's active set",
                code="bgp_handover_set_mismatch",
            )

        successor_row = conn.execute(
            "SELECT * FROM bgp_plans WHERE rowid = ?",
            (successor_cursor.lastrowid,),
        ).fetchone()
        retired_row = conn.execute(
            "SELECT * FROM bgp_plans WHERE plan_id = ?",
            (active_plan_id,),
        ).fetchone()

        plans = [
            _plan_response(retired_row, replayed=False),
            _plan_response(successor_row, replayed=False),
        ]

    return {
        "pool_id": pool_id,
        "released": target["cidr"],
        "request_id": request_id,
        "plans": plans,
    }
