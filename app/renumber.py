"""Whole-pool renumbering: move a pool and its contents to a new CIDR.

``POST /pools/{id}/renumber`` moves a pool to a new CIDR of the *same*
prefix length.  Every exclusion and allocation is translated by the same
address delta (its offset relative to the old pool's first address), so the
internal layout of the pool is preserved exactly: prefix lengths, record
ids, ``request_id`` values and ``created_at`` timestamps are all kept; only
the CIDR strings change.  Afterwards pool queries, ``request_id`` idempotent
retries and releases all resolve through the new addresses.

The whole operation runs inside one ``BEGIN IMMEDIATE`` write transaction, so
the checks and the rewrite share one serialised view of the database.  It is
accepted only when every check below passes:

* the new CIDR is a strict IPv4 network with the same prefix length as the
  existing pool;
* the new pool range does not overlap any other existing pool;
* every translated exclusion/allocation still lies completely inside the
  new pool and the translated ranges stay pairwise disjoint;
* the pool has no ``planned`` or ``active`` BGP announcement plan (a plan
  pins a pool-state digest that the move would invalidate);
* the pool never appears in a completed policy-gated batch reservation or
  cross-pool allocation migration record, so replaying those idempotency
  keys can never return pre-move addresses.

Any failure rolls the transaction back: the pool and every one of its rows
stay exactly as they were.
"""

from __future__ import annotations

import ipaddress
import sqlite3

from .cidrmath import parse_network
from .db import write_transaction
from .service import (
    ApiError,
    _pool_allocations,
    _pool_exclusions,
    _row_pool,
)

_IPV4_MAX = int(ipaddress.IPv4Address("255.255.255.255"))


def _shift_network(
    net: ipaddress.IPv4Network,
    old_start: int,
    new_start: int,
) -> ipaddress.IPv4Network:
    """Translate ``net`` by the pool-start delta, keeping its prefix length."""

    offset = int(net.network_address) - old_start
    shifted_int = new_start + offset
    if not 0 <= shifted_int <= _IPV4_MAX:
        raise ApiError(
            422,
            f"translated network of {net} would leave the IPv4 address range",
            code="renumber_shifted_outside_pool",
        )
    return ipaddress.IPv4Network((shifted_int, net.prefixlen))


def renumber_pool(pool_id: int, new_cidr: str) -> dict:
    """Move ``pool_id`` to ``new_cidr`` inside one serialised write transaction.

    Validation (blockers, overlap, translation) and the CIDR rewrites all run
    inside the same ``BEGIN IMMEDIATE`` transaction, so any failure leaves the
    pool and all of its exclusions and allocations byte-for-byte unchanged.
    """

    try:
        new_net = parse_network(new_cidr)
    except ValueError as exc:
        raise ApiError(422, f"invalid pool CIDR: {exc}", code="invalid_cidr")

    with write_transaction() as conn:
        pool = _row_pool(conn, pool_id)
        old_cidr = pool["cidr"]
        old_net = parse_network(old_cidr)

        if new_net.prefixlen != old_net.prefixlen:
            raise ApiError(
                422,
                f"new pool CIDR {new_net} has prefix length /{new_net.prefixlen}, "
                f"but pool {pool_id} is {old_net} (/{old_net.prefixlen}); "
                "renumbering requires the same prefix length",
                code="renumber_prefix_length_changed",
            )

        # A planned or active BGP plan pins a digest of (pool CIDR,
        # exclusions, allocations); rewriting any of those would leave the
        # plan permanently stale, so refuse while one exists.  Retired plans
        # are history and no longer pin state.
        plan = conn.execute(
            "SELECT plan_id, status FROM bgp_plans "
            "WHERE pool_id = ? AND status IN ('planned', 'active') LIMIT 1",
            (pool_id,),
        ).fetchone()
        if plan is not None:
            raise ApiError(
                409,
                f"pool {pool_id} has a {plan['status']} BGP plan "
                f"({plan['plan_id']!r}); cancel or finish it before renumbering "
                "the pool",
                code="renumber_blocked_by_bgp_plan",
            )

        # Completed batches are replayable idempotency records whose stored
        # result carries the old CIDRs.  A history replay after the move would
        # hand out pre-move addresses, so the move is refused instead.
        batch = conn.execute(
            "SELECT batch_id FROM reservation_batches WHERE pool_id = ? LIMIT 1",
            (pool_id,),
        ).fetchone()
        if batch is not None:
            raise ApiError(
                409,
                f"pool {pool_id} appears in completed reservation batch "
                f"{batch['batch_id']!r}; its idempotent replay must keep returning "
                "the original addresses, so the pool cannot be renumbered",
                code="renumber_blocked_by_reservation_batch",
            )

        # The pool may be either the source or the target of a recorded
        # cross-pool migration; either replay pins addresses that the move
        # would invalidate.
        migration = conn.execute(
            "SELECT operation_id, source_pool_id, target_pool_id "
            "FROM allocation_migrations "
            "WHERE source_pool_id = ? OR target_pool_id = ? LIMIT 1",
            (pool_id, pool_id),
        ).fetchone()
        if migration is not None:
            role = (
                "the source of"
                if migration["source_pool_id"] == pool_id
                else "the target of"
            )
            raise ApiError(
                409,
                f"pool {pool_id} is {role} completed allocation migration "
                f"{migration['operation_id']!r}; its idempotent replay must keep "
                "returning the original addresses, so the pool cannot be renumbered",
                code="renumber_blocked_by_allocation_migration",
            )

        # The new range must not overlap any other pool.  When the new CIDR
        # equals the current one this can never fire (the only matching pool
        # is this one).
        other_pools = conn.execute(
            "SELECT id, cidr FROM pools WHERE id != ?",
            (pool_id,),
        ).fetchall()
        for other in other_pools:
            other_net = parse_network(other["cidr"])
            if new_net.overlaps(other_net):
                raise ApiError(
                    409,
                    f"new pool CIDR {new_net} overlaps existing pool "
                    f"{other['id']} ({other_net})",
                    code="renumber_pool_overlap",
                )

        # Translate every exclusion and allocation by the pool-start delta.
        # Same-sized pools make the shift a bijection, so containment and
        # disjointness necessarily survive; both are still re-proved
        # explicitly so malformed stored state can never be half-moved.
        old_start = int(old_net.network_address)
        new_start = int(new_net.network_address)

        exclusion_rows = _pool_exclusions(conn, pool_id)
        allocation_rows = _pool_allocations(conn, pool_id)

        exclusion_moves: list[tuple[sqlite3.Row, ipaddress.IPv4Network]] = []
        for row in exclusion_rows:
            shifted = _shift_network(parse_network(row["cidr"]), old_start, new_start)
            if not shifted.subnet_of(new_net):
                raise ApiError(
                    422,
                    f"translated exclusion {shifted} (from {row['cidr']}) is not "
                    f"inside new pool {new_net}",
                    code="renumber_shifted_outside_pool",
                )
            exclusion_moves.append((row, shifted))

        allocation_moves: list[tuple[sqlite3.Row, ipaddress.IPv4Network]] = []
        for row in allocation_rows:
            shifted = _shift_network(parse_network(row["cidr"]), old_start, new_start)
            if not shifted.subnet_of(new_net):
                raise ApiError(
                    422,
                    f"translated allocation {shifted} (from {row['cidr']}, "
                    f"request_id {row['request_id']!r}) is not inside new pool "
                    f"{new_net}",
                    code="renumber_shifted_outside_pool",
                )
            allocation_moves.append((row, shifted))

        all_shifted = [net for _, net in exclusion_moves] + [
            net for _, net in allocation_moves
        ]
        all_shifted.sort(key=lambda n: int(n.network_address))
        for earlier, later in zip(all_shifted, all_shifted[1:]):
            if earlier.overlaps(later):
                raise ApiError(
                    409,
                    f"translated ranges {earlier} and {later} overlap after "
                    f"renumbering pool {pool_id}",
                    code="renumber_shifted_conflict",
                )

        # All checks passed: rewrite the CIDRs in place.  Row ids,
        # request_ids, prefix lengths and created_at are untouched, so the
        # rows keep their identity and only the pool CIDR and offsets change.
        try:
            conn.execute(
                "UPDATE pools SET cidr = ? WHERE id = ?",
                (str(new_net), pool_id),
            )
            conn.executemany(
                "UPDATE exclusions SET cidr = ? WHERE id = ? AND pool_id = ?",
                [
                    (str(shifted), row["id"], pool_id)
                    for row, shifted in exclusion_moves
                ],
            )
            conn.executemany(
                "UPDATE allocations SET cidr = ? WHERE id = ? AND pool_id = ?",
                [
                    (str(shifted), row["id"], pool_id)
                    for row, shifted in allocation_moves
                ],
            )
        except sqlite3.IntegrityError:
            raise ApiError(
                409,
                f"renumbering pool {pool_id} to {new_net} conflicts with "
                "existing data",
                code="renumber_conflict",
            )

        exclusions = [
            {"old_cidr": row["cidr"], "new_cidr": str(shifted)}
            for row, shifted in sorted(
                exclusion_moves,
                key=lambda move: int(move[1].network_address),
            )
        ]
        allocations = [
            {
                "allocation_id": row["id"],
                "request_id": row["request_id"],
                "old_cidr": row["cidr"],
                "new_cidr": str(shifted),
                "prefix_length": row["prefix_length"],
            }
            for row, shifted in sorted(
                allocation_moves,
                key=lambda move: int(move[1].network_address),
            )
        ]

    return {
        "pool_id": pool_id,
        "old_cidr": old_cidr,
        "new_cidr": str(new_net),
        "exclusions": exclusions,
        "allocations": allocations,
    }
