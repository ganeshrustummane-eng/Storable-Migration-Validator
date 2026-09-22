"""Self-check for the Postgres connector's timeout hardening.
Run: python Project/db/test_postgres.py
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.postgres import Postgres


def test_connect_applies_timeout_and_statement_timeout():
    pg = Postgres("db", "user", "pw", "host", 5432, schema="public")
    with patch("db.postgres.psycopg2.connect") as mock_connect:
        pg.connect()
        _, kwargs = mock_connect.call_args
        assert kwargs["connect_timeout"] == Postgres.CONNECT_TIMEOUT_SECONDS
        assert f"statement_timeout={Postgres.STATEMENT_TIMEOUT_SECONDS * 1000}" in kwargs["options"]
        assert "search_path=public" in kwargs["options"]
    print("test_connect_applies_timeout_and_statement_timeout: OK")


def test_connect_without_schema_still_sets_statement_timeout():
    pg = Postgres("db", "user", "pw", "host", 5432)
    with patch("db.postgres.psycopg2.connect") as mock_connect:
        pg.connect()
        _, kwargs = mock_connect.call_args
        assert "statement_timeout=" in kwargs["options"]
        assert "search_path" not in kwargs["options"]
    print("test_connect_without_schema_still_sets_statement_timeout: OK")


def test_execute_query_unchanged_behavior():
    pg = Postgres("db", "user", "pw", "host", 5432)
    fake_conn = MagicMock()
    fake_cur = MagicMock()
    fake_cur.fetchall.return_value = [(1, "a"), (2, "b")]
    fake_cur.description = [("id",), ("name",)]
    fake_conn.cursor.return_value = fake_cur
    pg.connect = MagicMock(return_value=fake_conn)

    df = pg.execute_query("select id, name from t")
    assert list(df.columns) == ["id", "name"]
    assert len(df) == 2
    fake_cur.close.assert_called_once()
    fake_conn.close.assert_called_once()
    print("test_execute_query_unchanged_behavior: OK")


if __name__ == "__main__":
    test_connect_applies_timeout_and_statement_timeout()
    test_connect_without_schema_still_sets_statement_timeout()
    test_execute_query_unchanged_behavior()
    print("All Postgres connector checks passed.")
