"""Self-check for the Snowflake connector's timeout hardening.
Run: python Project/db/test_snowflake.py
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# This script's own directory (Project/db) is auto-added to sys.path[0] by
# Python and contains snowflake.py, which shadows the real top-level
# `snowflake` package that db/snowflake.py itself needs to import. Strip it
# before adding Project/ so `import snowflake.connector` resolves correctly.
_THIS_DIR = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p != _THIS_DIR]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.snowflake import Snowflake


def test_connect_applies_timeouts_and_statement_timeout():
    sf = Snowflake("acct", "user", "pw", "db", "schema")
    with patch("db.snowflake.snowflake.connector.connect") as mock_connect:
        sf.connect()
        _, kwargs = mock_connect.call_args
        assert kwargs["login_timeout"] == Snowflake.LOGIN_TIMEOUT_SECONDS
        assert kwargs["network_timeout"] == Snowflake.NETWORK_TIMEOUT_SECONDS
        assert kwargs["session_parameters"]["STATEMENT_TIMEOUT_IN_SECONDS"] == Snowflake.STATEMENT_TIMEOUT_SECONDS
    print("test_connect_applies_timeouts_and_statement_timeout: OK")


def test_execute_query_unchanged_behavior():
    sf = Snowflake("acct", "user", "pw", "db", "schema")
    fake_cs = MagicMock()
    fake_cs.fetchall.return_value = [(1, "a"), (2, "b")]
    fake_cs.description = [("ID",), ("NAME",)]
    fake_cs.__enter__ = MagicMock(return_value=fake_cs)
    fake_cs.__exit__ = MagicMock(return_value=False)

    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cs
    fake_conn.__enter__ = MagicMock(return_value=fake_conn)
    fake_conn.__exit__ = MagicMock(return_value=False)

    sf.connect = MagicMock(return_value=fake_conn)
    df = sf.execute_query("select id, name from t")
    assert list(df.columns) == ["ID", "NAME"]
    assert len(df) == 2
    print("test_execute_query_unchanged_behavior: OK")


if __name__ == "__main__":
    test_connect_applies_timeouts_and_statement_timeout()
    test_execute_query_unchanged_behavior()
    print("All Snowflake connector checks passed.")
