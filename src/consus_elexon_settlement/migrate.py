"""Schema migrations.

Numbered SQL files in migrations/, applied once each, in order, inside a
transaction. No ORM and no Alembic: the schema is small enough that plain SQL
is the clearest thing an assessor can read against the IDD.

Three properties that matter for a qualified system:

  * Applied migrations are recorded with a checksum. Editing a file that has
    already run is an error, not a silent divergence between environments.
  * An advisory lock serialises concurrent runners, so two Cloud Run
    instances starting at once cannot both apply 0002.
  * Each file runs in its own transaction. A failure leaves the database at
    the last good migration rather than half-applied.

Change control is part of the qualification assessment. This file plus the
migrations directory is the answer to it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from psycopg import Connection

MIGRATIONS = Path(__file__).with_name("migrations")
FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

# Arbitrary but fixed: any advisory lock key works provided every runner uses
# the same one.
LOCK_KEY = 0x_EC_7A_A0_01


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def discover(directory: Path = MIGRATIONS) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = FILENAME.match(path.name)
        if not match:
            raise MigrationError(f"{path.name} is not named NNNN_description.sql")
        migrations.append(
            Migration(version=int(match.group(1)), name=path.stem, sql=path.read_text())
        )

    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration versions: {versions}")
    return migrations


def _ensure_table(conn: Connection) -> None:
    with conn.transaction():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migration (
                version    int PRIMARY KEY,
                name       text NOT NULL,
                checksum   text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )


def applied(conn: Connection) -> dict[int, tuple[str, str]]:
    _ensure_table(conn)
    rows = conn.execute("SELECT version, name, checksum FROM schema_migration").fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def migrate(conn: Connection, directory: Path = MIGRATIONS) -> list[Migration]:
    """Apply outstanding migrations. Returns those applied.

    The connection must have no transaction in progress.
    """
    migrations = discover(directory)

    # Autocommit for the duration, so each `with conn.transaction()` block
    # below is a real transaction that commits on exit. Without this, a
    # lingering implicit transaction turns them into savepoints and nothing is
    # durable until the caller commits -- which would make a mid-run failure
    # roll back migrations that had already succeeded.
    previous_autocommit = conn.autocommit
    conn.commit()
    conn.autocommit = True

    # Session-level lock, not transaction-level: each migration commits
    # separately, so a transaction-scoped lock would release after the first.
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        history = applied(conn)

        for migration in migrations:
            if migration.version not in history:
                continue
            name, checksum = history[migration.version]
            if checksum != migration.checksum:
                raise MigrationError(
                    f"migration {migration.version} ({name}) has changed since it was "
                    f"applied. Add a new migration instead of editing history."
                )

        outstanding = [m for m in migrations if m.version not in history]
        for migration in outstanding:
            # One transaction per file. A failure in 0002 leaves 0001 applied
            # rather than rolling the database back to nothing.
            with conn.transaction():
                conn.execute(migration.sql)
                conn.execute(
                    "INSERT INTO schema_migration (version, name, checksum) "
                    "VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        conn.autocommit = previous_autocommit

    return outstanding


def current_version(conn: Connection) -> int | None:
    _ensure_table(conn)
    row = conn.execute("SELECT max(version) FROM schema_migration").fetchone()
    return row[0] if row else None