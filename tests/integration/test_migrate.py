"""Tests for the migration runner."""

import os

import psycopg
import pytest

from consus_elexon_settlement import migrate

DSN = os.environ.get("SETTLEMENT_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SETTLEMENT_TEST_DSN not set")


@pytest.fixture
def conn():
    connection = psycopg.connect(DSN)
    with connection.transaction():
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    yield connection
    connection.close()


def write(directory, version: str, body: str) -> None:
    (directory / f"{version}.sql").write_text(body)


def test_applies_in_order(conn, tmp_path):
    write(tmp_path, "0001_first", "CREATE TABLE a (id int PRIMARY KEY);")
    write(tmp_path, "0002_second", "CREATE TABLE b (id int REFERENCES a(id));")

    applied = migrate.migrate(conn, tmp_path)

    assert [m.version for m in applied] == [1, 2]
    assert migrate.current_version(conn) == 2


def test_is_idempotent(conn, tmp_path):
    write(tmp_path, "0001_first", "CREATE TABLE a (id int);")
    migrate.migrate(conn, tmp_path)
    assert migrate.migrate(conn, tmp_path) == []


def test_only_outstanding_are_applied(conn, tmp_path):
    write(tmp_path, "0001_first", "CREATE TABLE a (id int);")
    migrate.migrate(conn, tmp_path)

    write(tmp_path, "0002_second", "CREATE TABLE b (id int);")
    assert [m.version for m in migrate.migrate(conn, tmp_path)] == [2]


def test_editing_applied_migration_is_rejected(conn, tmp_path):
    write(tmp_path, "0001_first", "CREATE TABLE a (id int);")
    migrate.migrate(conn, tmp_path)

    write(tmp_path, "0001_first", "CREATE TABLE a (id bigint);")
    with pytest.raises(migrate.MigrationError, match="has changed"):
        migrate.migrate(conn, tmp_path)


def test_failure_leaves_earlier_migrations_applied(conn, tmp_path):
    write(tmp_path, "0001_first", "CREATE TABLE a (id int);")
    write(tmp_path, "0002_broken", "THIS IS NOT SQL;")

    with pytest.raises(psycopg.Error):
        migrate.migrate(conn, tmp_path)

    conn.rollback()
    # 0001 stays applied; the database sits at the last good version.
    assert migrate.current_version(conn) == 1


def test_bad_filename_is_rejected(conn, tmp_path):
    write(tmp_path, "initial", "CREATE TABLE a (id int);")
    with pytest.raises(migrate.MigrationError, match="not named"):
        migrate.migrate(conn, tmp_path)


def test_duplicate_version_is_rejected(conn, tmp_path):
    write(tmp_path, "0001_first", "CREATE TABLE a (id int);")
    write(tmp_path, "0001_also_first", "CREATE TABLE b (id int);")
    with pytest.raises(migrate.MigrationError, match="duplicate"):
        migrate.migrate(conn, tmp_path)


def test_real_migrations_apply_cleanly(conn):
    """The shipped migrations, against an empty database."""
    applied = migrate.migrate(conn)
    assert [m.name for m in applied][0] == "0001_initial"

    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    names = {t[0] for t in tables}
    assert {"channel", "outbound_file", "notification", "notification_period",
            "wman", "inbound_file"} <= names