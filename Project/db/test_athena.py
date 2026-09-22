"""Self-check for the Athena connector's timeout + pagination fix.
Run: python Project/db/test_athena.py
"""
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.athena import Athena


def _col_info():
    return [{"Name": "ID"}, {"Name": "VAL"}]


def test_paginates_past_1000_rows():
    athena = Athena("us-east-1", "db", "s3://bucket/out/")
    client = MagicMock()
    client.start_query_execution.return_value = {"QueryExecutionId": "q1"}
    client.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
    }
    page1 = {
        "ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": _col_info()},
            "Rows": [{"Data": [{"VarCharValue": "ID"}, {"VarCharValue": "VAL"}]}]
            + [{"Data": [{"VarCharValue": str(i)}, {"VarCharValue": "x"}]} for i in range(1000)],
        },
        "NextToken": "tok2",
    }
    page2 = {
        "ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": _col_info()},
            "Rows": [{"Data": [{"VarCharValue": str(i)}, {"VarCharValue": "y"}]} for i in range(1000, 1500)],
        }
    }
    client.get_query_results.side_effect = [page1, page2]
    athena.connect = MagicMock(return_value=client)

    df = athena.execute_query("select * from t")
    assert len(df) == 1500, f"expected 1500 rows across both pages, got {len(df)}"
    assert list(df.columns) == ["ID", "VAL"]
    print("test_paginates_past_1000_rows: OK")


def test_stuck_query_times_out():
    athena = Athena("us-east-1", "db", "s3://bucket/out/")
    athena.MAX_POLL_SECONDS = 4
    athena.POLL_INTERVAL_SECONDS = 2
    client = MagicMock()
    client.start_query_execution.return_value = {"QueryExecutionId": "q2"}
    client.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "RUNNING"}}
    }
    athena.connect = MagicMock(return_value=client)

    try:
        athena.execute_query("select * from t")
        raise AssertionError("expected TimeoutError for a stuck query")
    except TimeoutError:
        pass
    client.stop_query_execution.assert_called_once_with(QueryExecutionId="q2")
    print("test_stuck_query_times_out: OK")


if __name__ == "__main__":
    test_paginates_past_1000_rows()
    test_stuck_query_times_out()
    print("All Athena connector checks passed.")
