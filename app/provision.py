"""One-shot address provisioning together with a BGP announcement plan.

``POST /pools/{id}/provisions`` allocates one aligned free subnet and stores
the accompanying BGP announcement plan in the *same* ``BEGIN IMMEDIATE``
write transaction.  The caller submits:

* ``request_id`` / ``prefix_length`` -- the ordinary allocation request;
* ``plan_id`` -- the plan idempotency key;
* ``relationships`` -- AS relationships used by the same path audit as the
  ``/bgp/announcements/audit`` and ``/bgp-plans`` endpoints;
* ``announcements`` -- the announcements covering every *current* allocation
  of the pool;
* ``new_announcement`` -- an id and an ``as_path`` only: its ``prefix`` is
  completed server-side with the CIDR chosen for the new allocation.

Inside the single write transaction the service:

1. detects replays/takeovers: a ``plan_id`` stored by this endpoint replays
   when every submitted parameter is identical and conflicts otherwise; a
   ``plan_id`` already used by a standalone plan, or a ``request_id`` already
   used by any allocation (including standalone allocations and reservation
   batches), cannot be taken over;
2. selects the lowest-start-address aligned free subnet, exactly like the
  standalone allocation endpoint;
3. completes ``new_announcement`` with the selected CIDR and audits the full
   announcement set (current announcements plus the new one) with the
   existing stateless path audit;
4. applies the existing plan coverage rules against the pool state *including
   the freshly inserted allocation*: every announcement prefix inside the
   pool and clear of exclusions, every allocation covered by a ``valid``
   announcement, and a single origin per allocation;
5. only when every check passes, inserts the ordinary allocation row (fully
   visible to every other endpoint), the immutable ``planned`` BGP plan
   (pinned to the post-allocation pool-state summary) and the provision
   linkage record, then commits.

A pool with no free aligned subnet (``pool_exhausted``), an audit/coverage
failure or an origin conflict rolls the transaction back: neither the
allocation nor the plan is left behind, and neither idempotency key is
occupied.

Releasing the allocation (``DELETE /pools/{id}/allocations/{request_id}``)
removes the ordinary allocation row, but the provision record and the plan
deliberately survive: ``address_provisions.allocation_id`` is only a
historical reference, not a cascading foreign key.  Retrying the same
``plan_id`` with identical parameters therefore still replays the stored
original allocation (its old row id and CIDR, which is no longer occupied and
never appears among the pool's current allocations), announcement and plan,
with the plan's *current* status.  No address is re-allocated and the freed
request_id stays free for an ordinary allocation; a later unrelated allocation
that happens to reuse that request_id does not perturb the stored replay.
Changing any parameter, or taking the plan_id with a different provision or a
standalone plan, keeps returning the usual conflict.

The stored plan is an ordinary ``bgp_plans`` row: the standalone plan create,
activate and handover endpoints keep working unchanged, and activating a
provision-created plan still re-verifies the latest pool state inside its own
write transaction.
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
)
from .cidrmath import lowest_aligned, parse_network
from .db import write_transaction
from .schemas import BgpAnnouncement
from .service import (
    ApiError,
    _free_networks,
    _pool_allocations,
    _pool_exclusions,
    _row_pool,
    utc_now,
)


def _provision_params(
    pool_id: int,
    request_id: str,
    prefix_length: int,
    plan_id: str,
    relationships,
    announcements,
    new_announcement,
    chosen_cidr: str,
) -> str:
    """Canonical fingerprint of a provision request.

    The plan-side fingerprint (relationships deduplicated/sorted,
    announcements sorted by id with the server-completed prefix) is embedded
    alongside the allocation parameters and the plan key, so a retry that
    changes any of them is detected even before the pool is touched.
    """

    completed_new = BgpAnnouncement(
        id=new_announcement.id, prefix=chosen_cidr, as_path=list(new_announcement.as_path)
    )
    plan_params = _canonical_params(relationships, [*announcements, completed_new])
    params = {
        "pool_id": pool_id,
        "request_id": request_id,
        "prefix_length": prefix_length,
        "plan_id": plan_id,
        "plan": plan_params,
    }
    return json.dumps(params, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def provision_address(
    pool_id: int,
    request_id: str,
    prefix_length: int,
    plan_id: str,
    relationships,
    announcements,
    new_announcement,
) -> dict:
    """Allocate one subnet and store its BGP plan atomically.

    ``relationships``/``announcements``/``new_announcement`` are pydantic
    objects from the request models; ``new_announcement`` exposes ``id`` and
    ``as_path`` but no prefix yet.
    """

    if not 0 <= prefix_length <= 32:
        raise ApiError(
            422,
            "prefix_length must be between 0 and 32",
            code="invalid_prefix",
        )

    # Run the path audit on the caller-supplied announcements before opening
    # the transaction, exactly like standalone plan creation: request-level
    # failures (relation conflicts, unknown edges, bad prefixes, empty paths,
    # duplicate ids) surface as 422 without touching the database.  The
    # completed new announcement is audited again below, inside the
    # transaction, together with the full set.
    try:
        audit(relationships, announcements)
    except BgpAuditError as exc:
        raise ApiError(422, exc.detail, code=exc.code)

    # Everything -- takeover checks, lowest-free selection, plan audit and
    # coverage validation, the allocation insert, the plan insert and the
    # provision linkage insert -- happens inside one immediate write
    # transaction.  Any failure rolls all three writes back together.
    with write_transaction() as conn:
        previous = conn.execute(
            "SELECT * FROM address_provisions WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()

        # The pool must exist before parameters can be compared against a
        # replay (the fingerprint embeds pool-scoped, canonical prefixes), so
        # take the 404 first.
        pool = _row_pool(conn, pool_id)
        pool_net = parse_network(pool["cidr"])

        if previous is not None:
            # Re-derive what the new-announcement prefix was from the stored
            # result to compare the full fingerprint; a replay must match it.
            stored = json.loads(previous["result"])
            stored_cidr = stored["new_announcement"]["prefix"]
            params_json = _provision_params(
                pool_id,
                request_id,
                prefix_length,
                plan_id,
                relationships,
                announcements,
                new_announcement,
                stored_cidr,
            )
            if (
                previous["pool_id"] != pool_id
                or previous["request_id"] != request_id
                or previous["params"] != params_json
            ):
                raise ApiError(
                    409,
                    f"plan_id {plan_id!r} already provisioned with different "
                    "parameters",
                    code="provision_id_conflict",
                )
            # Reflect the plan's current status: the plan created here is an
            # ordinary bgp_plans row and may have been activated or retired by
            # the standalone activate/handover endpoints since the provision
            # committed.  The allocation's identity never changes.
            current_plan = conn.execute(
                "SELECT * FROM bgp_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            stored["plan"] = _plan_response(current_plan, replayed=True)
            return _stamped(stored, replayed=True)

        # Fresh submission: neither idempotency key may have been taken by
        # another operation.  The allocations UNIQUE(request_id) constraint
        # additionally guards concurrent writers.
        existing_alloc = conn.execute(
            "SELECT * FROM allocations WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if existing_alloc is not None:
            raise ApiError(
                409,
                f"request_id {request_id!r} is already used by allocation "
                f"{existing_alloc['cidr']} in pool {existing_alloc['pool_id']}; "
                "it cannot be taken over by a provision",
                code="request_id_conflict",
            )
        existing_plan = conn.execute(
            "SELECT pool_id FROM bgp_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if existing_plan is not None:
            raise ApiError(
                409,
                f"plan_id {plan_id!r} is already used by a BGP plan of pool "
                f"{existing_plan['pool_id']}; it cannot be taken over by a "
                "provision",
                code="bgp_plan_id_conflict",
            )

        exclusions = [parse_network(r["cidr"]) for r in _pool_exclusions(conn, pool_id)]
        allocation_rows = _pool_allocations(conn, pool_id)
        allocations = [parse_network(r["cidr"]) for r in allocation_rows]

        # Same selection rule as a standalone allocation: the lowest
        # start-address aligned free subnet outside exclusions and current
        # allocations.
        free = _free_networks(pool_net, exclusions, allocations)
        chosen = lowest_aligned(free, prefix_length)
        if chosen is None:
            raise ApiError(
                409,
                f"no contiguous free /{prefix_length} subnet in pool {pool_net}",
                code="pool_exhausted",
            )

        completed_new = BgpAnnouncement(
            id=new_announcement.id,
            prefix=str(chosen),
            as_path=list(new_announcement.as_path),
        )
        try:
            results = audit(relationships, [*announcements, completed_new])["results"]
        except BgpAuditError as exc:
            raise ApiError(422, exc.detail, code=exc.code)

        now = utc_now()
        try:
            cursor = conn.execute(
                "INSERT INTO allocations "
                "(pool_id, cidr, request_id, prefix_length, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (pool_id, str(chosen), request_id, prefix_length, now),
            )
        except sqlite3.IntegrityError:
            # A concurrent writer committed the same request_id first.
            raise ApiError(
                409,
                f"request_id {request_id!r} is already used; it cannot be "
                "taken over by a provision",
                code="request_id_conflict",
            )
        allocation_id = cursor.lastrowid

        # Re-read the pool state with the new allocation already inserted and
        # validate the completed announcement set against it.  The plan is
        # pinned to exactly this post-commit state.
        post_allocation_rows = _pool_allocations(conn, pool_id)
        post_state = _pool_state(conn, pool_id)
        _validate_coverage(results, pool_net, exclusions, post_allocation_rows)
        state_digest = _state_digest(post_state)

        params_json = _provision_params(
            pool_id,
            request_id,
            prefix_length,
            plan_id,
            relationships,
            announcements,
            new_announcement,
            str(chosen),
        )
        plan_params_json = json.dumps(
            _canonical_params(relationships, [*announcements, completed_new])
        )

        try:
            plan_cursor = conn.execute(
                "INSERT INTO bgp_plans "
                "(plan_id, pool_id, params, audit_results, pool_state, state_digest, "
                "status, created_at, activated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, NULL)",
                (
                    plan_id,
                    pool_id,
                    plan_params_json,
                    json.dumps(results),
                    json.dumps(post_state),
                    state_digest,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            # A concurrent writer committed the same plan_id first.
            raise ApiError(
                409,
                f"plan_id {plan_id!r} is already used by a BGP plan; it "
                "cannot be taken over by a provision",
                code="bgp_plan_id_conflict",
            )
        plan_row = conn.execute(
            "SELECT * FROM bgp_plans WHERE rowid = ?",
            (plan_cursor.lastrowid,),
        ).fetchone()

        allocation_row = conn.execute(
            "SELECT * FROM allocations WHERE id = ?",
            (allocation_id,),
        ).fetchone()

        result = _build_result(
            pool_id=pool_id,
            allocation_row=allocation_row,
            plan_row=plan_row,
            results=results,
            chosen=str(chosen),
            new_announcement=completed_new,
            created_at=now,
        )

        conn.execute(
            "INSERT INTO address_provisions "
            "(plan_id, pool_id, allocation_id, request_id, params, result, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                pool_id,
                allocation_id,
                request_id,
                params_json,
                json.dumps(result),
                now,
            ),
        )

    return _stamped(result, replayed=False)


def _build_result(
    *,
    pool_id: int,
    allocation_row: sqlite3.Row,
    plan_row: sqlite3.Row,
    results: list[dict],
    chosen: str,
    new_announcement: BgpAnnouncement,
    created_at: str,
) -> dict:
    """Assemble the provision response, with the new announcement expanded."""

    new_result = next(r for r in results if r["id"] == new_announcement.id)
    return {
        "pool_id": pool_id,
        "allocation": {
            "id": allocation_row["id"],
            "pool_id": pool_id,
            "cidr": allocation_row["cidr"],
            "request_id": allocation_row["request_id"],
            "prefix_length": allocation_row["prefix_length"],
            "created_at": allocation_row["created_at"],
            "replayed": False,
        },
        "plan": _plan_response(plan_row, replayed=False),
        "new_announcement": {
            "id": new_result["id"],
            "prefix": chosen,
            "as_path": new_result["as_path"],
            "origin": new_result["origin"],
            "status": new_result["status"],
            "flags": new_result["flags"],
            "violating_hop": new_result["violating_hop"],
        },
        "created_at": created_at,
        "replayed": False,
    }


def _stamped(result: dict, replayed: bool) -> dict:
    """Return a stored result with every level's replay flag set consistently."""

    result = dict(result)
    result["replayed"] = replayed
    result["allocation"] = dict(result["allocation"], replayed=replayed)
    result["plan"] = dict(result["plan"], replayed=replayed)
    result["new_announcement"] = dict(result["new_announcement"])
    return result
