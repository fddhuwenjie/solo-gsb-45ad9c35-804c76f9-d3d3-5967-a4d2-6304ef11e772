"""Safe decommissioning of one allocation from a pool that keeps others.

``POST /pools/{id}/decommissions`` releases one in-use allocation and swaps
the pool's active BGP announcement plan for a successor inside a single
``BEGIN IMMEDIATE`` write transaction.  The caller submits:

* ``request_id`` -- the allocation to release;
* ``active_plan_id`` -- the pool's current active plan, to be retired;
* ``plan_id`` -- the idempotency key of the successor plan to store;
* ``relationships`` -- AS relationships, same format as the audit endpoint;
* ``announcements`` -- the successor announcements, which must cover every
  *remaining* allocation once the target is released.

Inside the one write transaction the service:

1. confirms the pool's active plan set is exactly ``active_plan_id`` and
   that the plan's pinned pool-state digest still matches the current
   state (any drift is a 409 conflict and nothing changes);
2. releases the target allocation;
3. re-validates the already audited successor announcements with the
   existing plan coverage rules against the post-release pool state -- no
   announcement may intersect the just-released CIDR, every remaining
   allocation must be covered by a ``valid`` announcement, and each
   allocation keeps a single origin;
4. only when every check passes, stores the successor plan (pinned to the
   post-release pool state) already ``active``, retires the old plan and
   commits.

A plan-id conflict, a state drift or an unqualified announcement rolls the
whole transaction back: the allocation is not released and no plan status
changes partially.  The retired plan and every provision record are kept
as history, so replaying a stored provision still returns its original
result without re-occupying the released address, and the standalone
allocation, release and plan endpoints keep working unchanged.
"""

from __future__ import annotations

import json
import sqlite3

from .bgpaudit import BgpAuditError, audit
from .bgpplan import (
    _canonical_params,
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


def decommission_allocation(
    pool_id: int,
    request_id: str,
    active_plan_id: str,
    plan_id: str,
    relationships,
    announcements,
) -> dict:
    """Release one allocation and atomically swap the pool's active plan.

    ``relationships``/``announcements`` are pydantic objects from the
    request models.  Every check and every write happens inside one
    ``BEGIN IMMEDIATE`` write transaction; any failure rolls the release
    and both plan changes back together.
    """

    # Run the existing path audit before opening the transaction, exactly
    # like standalone plan creation: request-level failures (relation
    # conflicts, unknown edges, bad prefixes, empty paths, duplicate ids)
    # surface as 422 without touching the database.
    try:
        results = audit(relationships, announcements)["results"]
    except BgpAuditError as exc:
        raise ApiError(422, exc.detail, code=exc.code)

    params_json = json.dumps(_canonical_params(relationships, announcements))

    with write_transaction() as conn:
        pool = _row_pool(conn, pool_id)
        pool_net = parse_network(pool["cidr"])

        # The named outgoing plan must exist and belong to this pool.
        old_plan = conn.execute(
            "SELECT * FROM bgp_plans WHERE plan_id = ?",
            (active_plan_id,),
        ).fetchone()
        if old_plan is None or old_plan["pool_id"] != pool_id:
            raise ApiError(
                404,
                f"BGP plan {active_plan_id!r} not found in pool {pool_id}",
                code="bgp_plan_not_found",
            )

        # The pool must carry exactly this one active plan: no missing, no
        # extra active plans (legacy databases may hold several).
        active_ids = {
            row["plan_id"]
            for row in conn.execute(
                "SELECT plan_id FROM bgp_plans "
                "WHERE pool_id = ? AND status = 'active'",
                (pool_id,),
            ).fetchall()
        }
        if active_ids != {active_plan_id}:
            raise ApiError(
                409,
                f"pool {pool_id} active BGP plans are {sorted(active_ids)}, "
                f"not exactly {active_plan_id!r}; the decommission must name "
                "the pool's only active plan",
                code="bgp_decommission_active_mismatch",
            )

        # The outgoing plan's pinned pool state must still be current:
        # any allocation/exclusion drift since it was pinned conflicts.
        _verify_plan_fresh(conn, pool_id, old_plan)

        # The allocation to release must exist in this pool.
        allocation = conn.execute(
            "SELECT * FROM allocations WHERE pool_id = ? AND request_id = ?",
            (pool_id, request_id),
        ).fetchone()
        if allocation is None:
            raise ApiError(
                404,
                f"request_id {request_id!r} not found in pool {pool_id}",
                code="allocation_not_found",
            )

        # The successor plan id must be free: a plan_id already stored by
        # any endpoint (standalone plan, provision or an earlier
        # decommission) cannot be taken over.
        existing_plan = conn.execute(
            "SELECT pool_id FROM bgp_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if existing_plan is not None:
            raise ApiError(
                409,
                f"plan_id {plan_id!r} is already used by a BGP plan of pool "
                f"{existing_plan['pool_id']}; it cannot be taken over by a "
                "decommission",
                code="bgp_plan_id_conflict",
            )

        # Release the target allocation; every check below evaluates the
        # post-release pool state inside the same transaction.
        conn.execute("DELETE FROM allocations WHERE id = ?", (allocation["id"],))
        released_net = parse_network(allocation["cidr"])

        # The successor announcements must not intersect the freed CIDR:
        # the decommissioned block is being withdrawn, not re-announced.
        for result in results:
            net = parse_network(result["prefix"])
            if net.overlaps(released_net):
                raise ApiError(
                    422,
                    f"announcement {result['id']!r} prefix {net} overlaps the "
                    f"released CIDR {released_net}",
                    code="bgp_announcement_overlaps_released",
                )

        # Re-validate coverage against the post-release state with the
        # existing plan rules: every announcement inside the pool and clear
        # of exclusions, every remaining allocation covered by a valid
        # announcement, a single origin per allocation.
        exclusions = [
            parse_network(row["cidr"]) for row in _pool_exclusions(conn, pool_id)
        ]
        remaining_rows = _pool_allocations(conn, pool_id)
        _validate_coverage(results, pool_net, exclusions, remaining_rows)

        # All checks passed: pin the successor plan to the post-release
        # pool state, already active (its pinned state is the state this
        # transaction commits, so the activation check holds by
        # construction), and retire the outgoing plan.
        post_state = _pool_state(conn, pool_id)
        state_digest = _state_digest(post_state)
        now = utc_now()
        try:
            cursor = conn.execute(
                "INSERT INTO bgp_plans "
                "(plan_id, pool_id, params, audit_results, pool_state, "
                "state_digest, status, created_at, activated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    plan_id,
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
            # Defence in depth: the pre-check above already ran under the
            # write lock, so this can only fire on a schema-level race.
            raise ApiError(
                409,
                f"plan_id {plan_id!r} conflicts with an existing plan",
                code="bgp_plan_id_conflict",
            )
        new_plan = conn.execute(
            "SELECT * FROM bgp_plans WHERE rowid = ?",
            (cursor.lastrowid,),
        ).fetchone()

        cursor = conn.execute(
            "UPDATE bgp_plans SET status = 'retired' "
            "WHERE plan_id = ? AND status = 'active'",
            (active_plan_id,),
        )
        if cursor.rowcount != 1:
            # Unreachable under BEGIN IMMEDIATE (the active set was read in
            # this transaction); kept as a guard so a status can never
            # change partially.
            raise ApiError(
                409,
                f"BGP plan {active_plan_id!r} is no longer active; a "
                "concurrent decommission changed the pool's active set",
                code="bgp_decommission_active_mismatch",
            )
        retired_plan = conn.execute(
            "SELECT * FROM bgp_plans WHERE plan_id = ?",
            (active_plan_id,),
        ).fetchone()

    return {
        "pool_id": pool_id,
        "released": str(released_net),
        "request_id": request_id,
        "plans": [
            _plan_response(retired_plan, replayed=False),
            _plan_response(new_plan, replayed=False),
        ],
    }
