from radfetch import db, store

ALL = ["0001_initial.sql", "0002_job_retry.sql", "0003_api_tokens.sql", "0004_run_image_digest.sql",
       "0005_notifications.sql", "0006_users.sql", "0007_results.sql"]


def fresh(conn):
    conn.execute("drop schema public cascade; create schema public")


def test_migrations_apply_in_order_and_only_once(conn):
    assert db.init_schema(conn) == []  # the fixture already applied everything
    fresh(conn)
    assert db.init_schema(conn) == ALL
    assert db.init_schema(conn) == []


def test_database_from_before_migrations_is_adopted(conn):
    """The first schema was applied without bookkeeping; running migrations on it must keep its data."""
    fresh(conn)
    db.init_schema(conn, through=1)
    conn.execute("drop table schema_migrations")
    conn.execute("insert into batches (id, name, flow) values (gen_random_uuid(), 'kept', 'seg')")
    assert db.init_schema(conn) == ALL
    assert conn.execute("select count(*) as n from batches").fetchone()["n"] == 1


def test_tokens_from_before_accounts_become_service_accounts(conn):
    fresh(conn)
    db.init_schema(conn, through=5)
    conn.execute(
        "insert into api_tokens (id, name, role, token_hash) values"
        " (gen_random_uuid(), 'ward-display', 'viewer', %s), (gen_random_uuid(), 'pipeline', 'operator', %s)",
        (store._token_hash("rf_old_viewer"), store._token_hash("rf_old_operator")),
    )
    db.init_schema(conn)
    user = store.user_for_token(conn, "rf_old_operator")
    assert (user["username"], user["role"]) == ("pipeline", "operator")
    assert store.user_for_token(conn, "rf_old_viewer")["role"] == "viewer"
    assert all(not u["has_password"] for u in store.list_users(conn))  # token-only accounts
