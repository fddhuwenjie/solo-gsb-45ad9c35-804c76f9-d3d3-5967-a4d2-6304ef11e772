"""Cross-pool allocation migration: move in-use subnets between pools.

``POST /pools/{id}/allocation-migrations`` moves a caller-supplied manifest
of existing allocations (identified by ``request_id``) from a source pool to
a target pool while preserving every allocation's CIDR, ``request_id``,
prefix length, row id and ``created_at`` -- only the owning ``pool_id``
changes.

The whole operation runs inside one ``BEGIN IMMEDIATE`` write transaction
that pins both pool states.  It is accepted only when every check below
passes:

* the manifest contains no duplicate ``request_id``;
* every manifest entry identifies an allocation that currently belongs to
  the source pool;
* every allocation CIDR is completely contained in the target pool;
* no moved CIDR overlaps a target-pool exclusion range or any other
  allocation already living in the target pool;
* neither the source nor the target pool has a ``planned`` or ``active``
  BGP announcement plan, so a pinned plan summary can never drift away from
  the pool state it describes.

Any failure rolls the transaction back: neither pool is touched.  On
success the allocations are re-homed and the input-order mapping is
returned.

``operation_id`` is the idempotency key: retrying with the same source and
target pools and the same manifest (input order is significant) replays the
original result even after the allocations have already been moved; the
same key with different parameters is a 409 conflict.
"""

from __future__ import annotations

import json
import sqlite3

from .cidrmath import parse_network
from .db import write_transaction
from .service import (
    ApiError,
    _pool_allocations,
    _pool_exclusions,
    _row_pool,
    utc_now,
)


def _migration_error(detail: str, code: str, status_code: int = 422) -> ApiError:
    return ApiError(status_code, detail, code=code)


def _canonical_params(
    source_pool_id: int, target_pool_id: int, request_ids: list[str]
) -> str:
    """Build the parameter fingerprint of a migration request.

    Manifest order is significant: it defines the returned input-order
    mapping, so replaying with the same entries in a different order is a
    different operation (and therefore a conflict).
    """

    params = {
        "source_pool_id": source_pool_id,
        "target_pool_id": target_pool_id,
        "request_ids": list(request_ids),
    }
    return json.dumps(params, separators=(",", ":"))


def _response(
    operation_id: str,
    migration_id: int,
    source_pool_id: int,
    target_pool_id: int,
    source_cidr: str,
    target_cidr: str,
    migrated: list[dict],
    created_at: str,
    replayed: bool,
) -> dict:
    return {
        "operation_id": operation_id,
        "migration_id": migration_id,
        "source_pool_id": source_pool_id,
        "target_pool_id": target_pool_id,
        "source_cidr": source_cidr,
        "target_cidr": target_cidr,
        "migrated": migrated,
        "created_at": created_at,
        "replayed": replayed,
    }


def migrate_allocations(
    source_pool_id: int,
    target_pool_id: int,
    operation_id: str,
    request_ids: list[str],
) -> dict:
    """Move the manifest allocations from the source pool to the target pool.

    Everything -- the idempotency decision, every containment/overlap/plan
    check and the ``pool_id`` update -- runs inside one immediate write
    transaction, so concurrent operations are serialised and any failure
    rolls the whole migration back.
    """

    params_json = _canonical_params(source_pool_id, target_pool_id, request_ids)

    with write_transaction() as conn:
        # Idempotency first: a completed migration re-homed its rows, so the
        # operation_id must be consulted before any ownership check.  A
        # matching record replays the stored result; any parameter change is
        # a conflict, and this wins over every other check.
        previous = conn.execute(
            "SELECT * FROM allocation_migrations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if previous is not None:
            if (
                previous["source_pool_id"] != source_pool_id
                or previous["target_pool_id"] != target_pool_id
                or previous["params"] != params_json
            ):
                raise ApiError(
                    409,
                    f"operation_id {operation_id!r} was already used for a "
                    "different allocation migration",
                    code="operation_id_conflict",
                )
            return _response(
                previous["operation_id"],
                previous["id"],
                previous["source_pool_id"],
                previous["target_pool_id"],
                previous["source_cidr"],
                previous["target_cidr"],
                json.loads(previous["result"]),
                previous["created_at"],
                replayed=True,
            )

        source = _row_pool(conn, source_pool_id)
        target = _row_pool(conn, target_pool_id)
        if source_pool_id == target_pool_id:
            raise _migration_error(
                f"source pool and target pool are both pool {source_pool_id}; "
                "an allocation migration requires two distinct pools",
                "migration_same_pool",
            )
        source_net = parse_network(source["cidr"])
        target_net = parse_network(target["cidr"])

        # The manifest must not repeat request_ids: the returned mapping is
        # keyed by input position, and a double entry would describe the same
        # allocation twice.
        seen: set[str] = set()
        for request_id in request_ids:
            if request_id in seen:
                raise _migration_error(
                    f"request_id {request_id!r} appears more than once in the "
                    "migration manifest",
                    "migration_duplicate_request_id",
                )
            seen.add(request_id)

        # Resolve every manifest entry to a current source-pool allocation.
        # request_id is globally unique, so a hit in another pool is a
        # definite ownership mismatch rather than an ambiguous reference.
        moving_rows: list[sqlite3.Row] = []
        for request_id in request_ids:
            row = conn.execute(
                "SELECT * FROM allocations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise _migration_error(
                    f"request_id {request_id!r} does not identify an existing "
                    "allocation",
                    "allocation_not_found",
                    status_code=404,
                )
            if row["pool_id"] != source_pool_id:
                raise _migration_error(
                    f"request_id {request_id!r} belongs to pool {row['pool_id']}, "
                    f"not source pool {source_pool_id}",
                    "migration_allocation_not_in_source",
                    status_code=409,
                )
            moving_rows.append(row)

        moving_nets = [parse_network(row["cidr"]) for row in moving_rows]

        # Every moved CIDR must fit completely inside the target pool.  The
        # manifest was already checked for duplicates, so its networks are
        # necessarily distinct -- a duplicate request_id can never reach
        # here.
        for net in moving_nets:
            if not net.subnet_of(target_net):
                raise _migration_error(
                    f"allocation {net} is not inside target pool {target_net}",
                    "migration_allocation_outside_target",
                )

        # Target exclusions keep their space forever: a moved allocation may
        # not land on one.
        target_exclusions = [
            parse_network(row["cidr"]) for row in _pool_exclusions(conn, target_pool_id)
        ]
        for net in moving_nets:
            if any(net.overlaps(excluded) for excluded in target_exclusions):
                raise _migration_error(
                    f"allocation {net} overlaps an exclusion range of target "
                    f"pool {target_pool_id}",
                    "migration_allocation_overlaps_exclusion",
                    status_code=409,
                )

        # The moved CIDRs must not overlap the target pool's remaining
        # allocations (every allocation that is not part of this manifest).
        other_target_rows = [
            row
            for row in _pool_allocations(conn, target_pool_id)
            if row["request_id"] not in seen
        ]
        for net in moving_nets:
            for row in other_target_rows:
                other_net = parse_network(row["cidr"])
                if net.overlaps(other_net):
                    raise _migration_error(
                        f"allocation {net} overlaps existing allocation "
                        f"{other_net} (request_id {row['request_id']!r}) in target "
                        f"pool {target_pool_id}",
                        "migration_allocation_overlap",
                        status_code=409,
                    )

        # A planned or active BGP plan pins a summary of its pool's
        # exclusions and allocations; re-homing rows would silently desync
        # either pool from that summary.  Refuse while any live plan exists
        # on either side; retired plans are history and no longer pin state.
        for pool_id, label in (
            (source_pool_id, "source"),
            (target_pool_id, "target"),
        ):
            plan = conn.execute(
                "SELECT plan_id, status FROM bgp_plans "
                "WHERE pool_id = ? AND status IN ('planned', 'active') LIMIT 1",
                (pool_id,),
            ).fetchone()
            if plan is not None:
                raise _migration_error(
                    f"{label} pool {pool_id} has a {plan['status']} BGP plan "
                    f"({plan['plan_id']!r}); cancel or finish it before migrating "
                    "allocations",
                    "migration_blocked_by_bgp_plan",
                    status_code=409,
                )

        # All checks passed: re-home the rows.  CIDR, request_id, prefix
        # length, row id and created_at are preserved verbatim; only the
        # owning pool changes.  The UNIQUE(request_id) constraint cannot
        # fire (those request_ids currently belong to the source pool), but
        # a defensive integrity guard still maps any other surprise to a
        # rolled-back conflict.
        now = utc_now()
        try:
            conn.executemany(
                "UPDATE allocations SET pool_id = ? WHERE id = ? AND pool_id = ?",
                [
                    (target_pool_id, row["id"], source_pool_id)
                    for row in moving_rows
                ],
            )

            migrated = [
                {
                    "request_id": row["request_id"],
                    "cidr": row["cidr"],
                    "prefix_length": row["prefix_length"],
                    "allocation_id": row["id"],
                    "replayed": False,
                }
                for row in moving_rows
            ]

            cursor = conn.execute(
                "INSERT INTO allocation_migrations "
                "(operation_id, source_pool_id, target_pool_id, source_cidr, "
                "target_cidr, params, result, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operation_id,
                    source_pool_id,
                    target_pool_id,
                    source["cidr"],
                    target["cidr"],
                    params_json,
                    json.dumps(migrated),
                    now,
                ),
            )
            migration_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            raise ApiError(
                409,
                f"allocation migration {operation_id!r} conflicts with existing data",
                code="migration_conflict",
            )

    return _response(
        operation_id,
        migration_id,
        source_pool_id,
        target_pool_id,
        source["cidr"],
        target["cidr"],
        migrated,
        now,
        replayed=False,
    )
