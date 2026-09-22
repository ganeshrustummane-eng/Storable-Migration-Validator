"""Self-check for the MSSQL connector's timeout hardening.
Run: python Project/db/test_mssqlserver.py
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.mssqlserver import Mssqlserver


def test_connect_applies_login_and_query_timeout():
    db = Mssqlserver("ODBC Driver 18 for SQL Server", "server", "db", "user", "pw")
    fake_conn = MagicMock()
    with patch("db.mssqlserver.pyodbc.connect", return_value=fake_conn) as mock_connect:
        conn = db.connect()
        _, kwargs = mock_connect.call_args
        assert kwargs["timeout"] == Mssqlserver.CONNECT_TIMEOUT_SECONDS
        assert conn.timeout == Mssqlserver.QUERY_TIMEOUT_SECONDS
    print("test_connect_applies_login_and_query_timeout: OK")


def test_execute_query_unchanged_behavior():
    db = Mssqlserver("ODBC Driver 18 for SQL Server", "server", "db", "user", "pw")
    fake_conn = MagicMock()
    fake_cur = MagicMock()
    fake_cur.fetchall.return_value = [(1, "a"), (2, "b")]
    fake_cur.description = [("id",), ("name",)]
    fake_conn.cursor.return_value = fake_cur
    db.connect = MagicMock(return_value=fake_conn)

    df = db.execute_query("select id, name from t")
    assert list(df.columns) == ["id", "name"]
    assert len(df) == 2
    fake_cur.close.assert_called_once()
    fake_conn.close.assert_called_once()
    print("test_execute_query_unchanged_behavior: OK")


if __name__ == "__main__":
    test_connect_applies_login_and_query_timeout()
    test_execute_query_unchanged_behavior()
    print("All MSSQL connector checks passed.")
