"""SQLite persistence: connection management, schema and transactions."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

# The database path is resolved lazily so tests can override it before the
# first connection is opened.
DB_PATH: str = os.environ.get("SUBNET_DB_PATH", "/data/subnets.db")

# SQLite returns SQLITE_BUSY immediately when another connection holds the
# write lock; wait this long before giving up.
BUSY_TIMEOUT_SECONDS = 10.0

# Confirmable BGP announcement plans.  Every row is immutable after
# creation: only ``status`` moves 'planned' -> 'active' (single activation
# or handover) and 'active' -> 'retired' (handover).  Deleting the pool
# cascades to its plans.  Kept as a constant so the schema migration below
# can rebuild the table with the identical definition.
_BGP_PLANS_DDL = """
            CREATE TABLE IF NOT EXISTS bgp_plans (
                plan_id          TEXT    PRIMARY KEY,
                pool_id          INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                params           TEXT    NOT NULL,
                audit_results    TEXT    NOT NULL,
                pool_state       TEXT    NOT NULL,
                state_digest     TEXT    NOT NULL,
                status           TEXT    NOT NULL CHECK (status IN ('planned', 'active', 'retired')),
                created_at       TEXT    NOT NULL,
                activated_at     TEXT
            );
"""

# One row per completed one-shot address provisioning.  Kept as a constant so
# the schema migration below can rebuild the table with the identical
# definition.  See the matching comment inside init_db for why allocation_id
# is deliberately a plain historical reference rather than an FK.
_ADDRESS_PROVISIONS_DDL = """
            CREATE TABLE IF NOT EXISTS address_provisions (
                plan_id        TEXT    PRIMARY KEY
                                  REFERENCES bgp_plans(plan_id) ON DELETE CASCADE,
                pool_id        INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                allocation_id  INTEGER NOT NULL,
                request_id     TEXT    NOT NULL,
                params         TEXT    NOT NULL,
                result         TEXT    NOT NULL,
                created_at     TEXT    NOT NULL
            );
"""


def _legacy_schema_flags(conn: sqlite3.Connection) -> tuple[bool, bool]:
    """Report which tables still carry a pre-upgrade definition.

    Returns ``(plans_legacy, provisions_legacy)``:

    * ``plans_legacy`` -- ``bgp_plans`` exists but its CHECK does not know the
      ``retired`` status (``'retired'`` is absent from the stored DDL);
    * ``provisions_legacy`` -- ``address_provisions`` exists but
      ``allocation_id`` still declares ``REFERENCES allocations`` (the old
      cascading foreign key).
    """

    plans_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'bgp_plans'"
    ).fetchone()
    plans_legacy = plans_row is not None and "'retired'" not in plans_row["sql"]

    provisions_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'address_provisions'"
    ).fetchone()
    provisions_legacy = (
        provisions_row is not None and "REFERENCES allocations" in provisions_row["sql"]
    )
    return plans_legacy, provisions_legacy


def _migrate_legacy_tables(conn: sqlite3.Connection) -> None:
    """Rebuild the pre-handover ``bgp_plans``/pre-release ``address_provisions``.

    The two rebuilds have to be coordinated inside **one** transaction with
    foreign-key enforcement suspended:

    * the old ``address_provisions.plan_id`` carries ``ON DELETE CASCADE`` to
      ``bgp_plans``, so rebuilding the plans table first and dropping its
      renamed copy would silently delete every provision row;
    * the old ``address_provisions.allocation_id`` cascades to
      ``allocations``, and rebuilding that table on its own left released
      provisions replayable but un-released ones at the mercy of the first
      rebuild.

    Both legacy tables are renamed (``legacy_alter_table`` keeps the stored
    foreign-key texts untouched), the current tables are created from the
    canonical DDL and every row is copied verbatim -- plans keep ``plan_id``
    and status (including pools with several active plans), provisions keep
    ``plan_id``, ``pool_id``, ``allocation_id`` and ``request_id``, so the
    plan/allocation/provision identity and linkage all survive.  Only then
    are the legacy copies dropped and the transaction committed; any failure
    rolls the whole rebuild back, leaving the database exactly as it was.
    Databases already on the current schema do nothing, so repeated
    ``init_db`` calls never touch the data.
    """

    plans_legacy, provisions_legacy = _legacy_schema_flags(conn)
    if not plans_legacy and not provisions_legacy:
        return  # fresh database or already migrated

    # PRAGMA foreign_keys/legacy_alter_table cannot be changed inside a
    # transaction; the connection is in autocommit mode here.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-read the schema with the write lock held: a concurrent
            # first-startup init may have committed the same rebuild moments
            # ago, in which case both flags now read False and there is
            # nothing left to do.
            plans_legacy, provisions_legacy = _legacy_schema_flags(conn)
            if plans_legacy:
                conn.execute("ALTER TABLE bgp_plans RENAME TO bgp_plans_legacy")
                conn.execute(_BGP_PLANS_DDL)
                conn.execute(
                    "INSERT INTO bgp_plans (plan_id, pool_id, params, audit_results, "
                    "pool_state, state_digest, status, created_at, activated_at) "
                    "SELECT plan_id, pool_id, params, audit_results, pool_state, "
                    "state_digest, status, created_at, activated_at "
                    "FROM bgp_plans_legacy"
                )
            if provisions_legacy:
                conn.execute(
                    "ALTER TABLE address_provisions RENAME TO address_provisions_legacy"
                )
                conn.execute(_ADDRESS_PROVISIONS_DDL)
                conn.execute(
                    "INSERT INTO address_provisions (plan_id, pool_id, allocation_id, "
                    "request_id, params, result, created_at) "
                    "SELECT plan_id, pool_id, allocation_id, request_id, params, result, "
                    "created_at FROM address_provisions_legacy"
                )
            # Dropping the legacy tables also drops their indexes (notably
            # idx_bgp_plans_pool and idx_address_provisions_request); the
            # executescript in init_db recreates them on the new tables.
            # Foreign keys are off, so no cascade can reach the freshly
            # copied rows.
            if plans_legacy:
                conn.execute("DROP TABLE bgp_plans_legacy")
            if provisions_legacy:
                conn.execute("DROP TABLE address_provisions_legacy")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(
        DB_PATH,
        timeout=BUSY_TIMEOUT_SECONDS,
        isolation_level=None,  # autocommit; transactions are managed explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers run while a writer holds the lock; allocation itself
    # is still serialised by the BEGIN IMMEDIATE write transaction.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        # Rebuild both legacy tables in one coordinated transaction before
        # anything else is created: running the plans rebuild on its own used
        # to cascade the old address_provisions rows away.
        _migrate_legacy_tables(conn)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pools (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cidr        TEXT    NOT NULL UNIQUE,
                created_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS exclusions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_id     INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                cidr        TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                UNIQUE (pool_id, cidr)
            );

            CREATE TABLE IF NOT EXISTS allocations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_id         INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                cidr            TEXT    NOT NULL,
                request_id      TEXT    NOT NULL UNIQUE,
                prefix_length   INTEGER NOT NULL,
                created_at      TEXT    NOT NULL
            );

            -- One row per completed restore; the primary key makes a repeated
            -- restore_id replay the original result instead of restoring twice.
            CREATE TABLE IF NOT EXISTS snapshot_restores (
                restore_id  TEXT    PRIMARY KEY,
                pool_id     INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                digest      TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
            );

            -- One row per completed pool partition.  The UNIQUE operation_id
            -- makes a retried partition replay the original result instead of
            -- splitting twice.  source_pool_id is a plain historical reference:
            -- the migration itself deletes the source pool row, so no FK.
            CREATE TABLE IF NOT EXISTS pool_migrations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id    TEXT    NOT NULL UNIQUE,
                source_pool_id  INTEGER NOT NULL,
                source_cidr     TEXT    NOT NULL,
                sub_pools       TEXT    NOT NULL,
                created_at      TEXT    NOT NULL
            );

            -- Confirmable BGP announcement plans; see _BGP_PLANS_DDL above.
            -- The migration in init_db rebuilds this table for databases
            -- created before the 'retired' status existed.
            """
            + _BGP_PLANS_DDL
            + """

            -- One row per completed policy-gated batch reservation.  The
            -- batch_id primary key makes a retried batch replay the original
            -- result instead of reserving twice; params is the canonical
            -- request fingerprint used to detect conflicting reuses.
            CREATE TABLE IF NOT EXISTS reservation_batches (
                batch_id    TEXT    PRIMARY KEY,
                pool_id     INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                params      TEXT    NOT NULL,
                result      TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
            );

            -- One row per completed cross-pool allocation migration.  The
            -- UNIQUE operation_id makes a retried migration replay the
            -- original result instead of moving the allocations twice.
            -- source_pool_id is a plain historical reference: the source
            -- pool of a later partition migration is deleted, so no FK;
            -- target_pool_id keeps the cascade so deleting the destination
            -- pool removes the record.  params is the canonical request
            -- fingerprint (manifest order is significant) and result holds
            -- the stored input-order mapping.
            CREATE TABLE IF NOT EXISTS allocation_migrations (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id     TEXT    NOT NULL UNIQUE,
                source_pool_id   INTEGER NOT NULL,
                target_pool_id   INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                source_cidr      TEXT    NOT NULL,
                target_cidr      TEXT    NOT NULL,
                params           TEXT    NOT NULL,
                result           TEXT    NOT NULL,
                created_at       TEXT    NOT NULL
            );

            -- One row per completed one-shot address provisioning.  Ties the
            -- regular allocation to the BGP announcement plan stored together
            -- with it in a single write transaction.  plan_id is both the
            -- idempotency key of the provision and an FK to the bgp_plans row
            -- it created, so a plan_id taken by a standalone plan (or by a
            -- previous provision) cannot be taken over here.  Deleting the
            -- linked plan or pool cascades the provision record away.
            --
            -- allocation_id is intentionally a plain historical reference,
            -- NOT a foreign key: releasing the provisioned allocation
            -- (DELETE /pools/{id}/allocations/{request_id}) must not delete
            -- this record.  The provision keeps being replayable by plan_id --
            -- it returns the stored original allocation, announcement and
            -- plan (with the plan's current status) instead of re-occupying
            -- the released address or colliding with a plan_id that is still
            -- occupied -- even if the freed request_id is later reused by an
            -- ordinary allocation.  The schema migration in init_db rebuilds
            -- older databases whose allocation_id carried ON DELETE CASCADE.
            """
            + _ADDRESS_PROVISIONS_DDL
            + """

            -- Fast lookup of all plans belonging to one pool.
            CREATE INDEX IF NOT EXISTS idx_allocations_pool
                ON allocations(pool_id);
            CREATE INDEX IF NOT EXISTS idx_exclusions_pool
                ON exclusions(pool_id);
            CREATE INDEX IF NOT EXISTS idx_bgp_plans_pool
                ON bgp_plans(pool_id);
            -- Fast takeover lookup by request_id for one-shot provisions.
            CREATE INDEX IF NOT EXISTS idx_address_provisions_request
                ON address_provisions(request_id);
            """
        )


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction() -> Iterator[sqlite3.Connection]:
    """Open an immediate write transaction.

    ``BEGIN IMMEDIATE`` acquires the reserved write lock up-front, so two
    concurrent allocations are serialised: the second one waits (up to
    ``busy_timeout``) instead of failing on commit.
    """

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def read_transaction() -> Iterator[sqlite3.Connection]:
    """Open a deferred (read) transaction for a consistent snapshot."""

    conn = _connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
