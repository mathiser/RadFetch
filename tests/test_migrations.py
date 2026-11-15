from radfetch import db


def test_migrations_apply_once_and_adopt_existing_schema(conn):
    assert db.init_schema(conn) == []  # the fixture already applied everything
    versions = [r["version"] for r in conn.execute("select version from schema_migrations order by 1").fetchall()]
    assert versions == sorted(versions) and versions[0] == 1

    # A database created before migrations existed has the tables but no bookkeeping.
    conn.execute("drop table schema_migrations")
    conn.execute("alter table jobs drop column retried_at")
    conn.execute("drop table api_tokens")
    conn.execute("insert into batches (id, name, flow) values (gen_random_uuid(), 'kept', 'seg')")
    assert db.init_schema(conn) == ["0001_initial.sql", "0002_job_retry.sql", "0003_api_tokens.sql"]
    assert conn.execute("select count(*) as n from batches").fetchone()["n"] == 1
