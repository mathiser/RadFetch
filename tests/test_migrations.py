from radfetch import db


def test_migrations_apply_once_and_adopt_existing_schema(conn):
    assert db.init_schema(conn) == []  # the fixture already applied everything
    versions = [r["version"] for r in conn.execute("select version from schema_migrations order by 1").fetchall()]
    assert versions == sorted(versions) and versions[0] == 1

    # A database created before migrations existed has the tables but no bookkeeping.
    conn.execute("drop table schema_migrations")
    conn.execute("alter table jobs drop column retried_at")
    conn.execute("drop table api_tokens")
    conn.execute("alter table runs drop column image_digest")
    conn.execute("drop table notifications; drop trigger jobs_notify_finished on jobs; drop function notify_job_finished")
    conn.execute("insert into batches (id, name, flow) values (gen_random_uuid(), 'kept', 'seg')")
    assert db.init_schema(conn) == ["0001_initial.sql", "0002_job_retry.sql", "0003_api_tokens.sql",
                                    "0004_run_image_digest.sql", "0005_notifications.sql"]
    assert conn.execute("select count(*) as n from batches").fetchone()["n"] == 1
