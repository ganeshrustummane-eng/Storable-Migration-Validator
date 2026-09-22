from db.base import Database
import pandas as pd
import boto3
import time


class Athena(Database):
    # ponytail: fixed ceiling, not per-table configurable; add a constructor
    # param / env var if a table's validation query genuinely needs longer.
    MAX_POLL_SECONDS = 1800  # 30 min — validation queries should never run longer
    POLL_INTERVAL_SECONDS = 2

    def __init__(self,AWS_REGION,ATHENA_DB,ATHENA_OUTPUT,ACCESS_KEY="",SECRET_KEY=""):
        self.AWS_REGION = AWS_REGION
        self.ATHENA_DB = ATHENA_DB
        self.ATHENA_OUTPUT = ATHENA_OUTPUT
        self.ACCESS_KEY = ACCESS_KEY
        self.SECRET_KEY = SECRET_KEY

    def connect(self):
        kwargs = dict(region_name=self.AWS_REGION)
        if self.ACCESS_KEY and self.SECRET_KEY:
            kwargs["aws_access_key_id"] = self.ACCESS_KEY
            kwargs["aws_secret_access_key"] = self.SECRET_KEY
        # Falls back to the default boto3 credential chain (AWS CLI profile,
        # instance role, etc.) when explicit keys aren't provided.
        session = boto3.Session(**kwargs)
        athena = session.client("athena")
        return athena
    
    def execute_query(self, query):
        athena = self.connect()
        response = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": self.ATHENA_DB},
        ResultConfiguration={"OutputLocation": self.ATHENA_OUTPUT},)

        query_execution_id = response["QueryExecutionId"]

        # Wait for completion — bounded, so a stuck/long-running query fails
        # loudly instead of blocking the validation run forever.
        elapsed = 0
        while True:
            status = athena.get_query_execution(QueryExecutionId=query_execution_id)
            state = status["QueryExecution"]["Status"]["State"]
            if state in ["SUCCEEDED", "FAILED", "CANCELLED"]:
                break
            if elapsed >= self.MAX_POLL_SECONDS:
                athena.stop_query_execution(QueryExecutionId=query_execution_id)
                raise TimeoutError(
                    f"Athena query {query_execution_id} did not finish within "
                    f"{self.MAX_POLL_SECONDS}s — cancelled."
                )
            time.sleep(self.POLL_INTERVAL_SECONDS)
            elapsed += self.POLL_INTERVAL_SECONDS

        if state != "SUCCEEDED":
            reason = status["QueryExecution"]["Status"].get("StateChangeReason", "No details")
            raise Exception(f"Query failed: {state} — {reason}")

        # Fetch result — get_query_results caps each page at 1000 rows, so
        # every page must be walked via NextToken or larger result sets are
        # silently truncated (wrong row counts / PK sets downstream).
        col_names = None
        rows = []
        next_token = None
        while True:
            kwargs = {"QueryExecutionId": query_execution_id}
            if next_token:
                kwargs["NextToken"] = next_token
            page = athena.get_query_results(**kwargs)

            page_rows = page["ResultSet"]["Rows"]
            if col_names is None:
                column_info = page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
                col_names = [col["Name"] for col in column_info]
                page_rows = page_rows[1:]  # header row only on the first page

            for row in page_rows:
                rows.append([c.get("VarCharValue", None) for c in row["Data"]])

            next_token = page.get("NextToken")
            if not next_token:
                break

        return pd.DataFrame(rows, columns=col_names)

    def execute_query_stream(self, query, chunksize=50_000):
        """Same get_query_results 1000-row/page pagination as execute_query — the
        1000-row/page ceiling is an AWS API limit this doesn't remove (that needs
        CTAS-to-S3, a separate change) — but pages are yielded as they arrive
        instead of accumulating the whole result in `rows` first, so the caller
        never holds more than ~chunksize rows in memory at once."""
        athena = self.connect()
        response = athena.start_query_execution(
            QueryString=query,
            QueryExecutionContext={"Database": self.ATHENA_DB},
            ResultConfiguration={"OutputLocation": self.ATHENA_OUTPUT},
        )
        query_execution_id = response["QueryExecutionId"]

        elapsed = 0
        while True:
            status = athena.get_query_execution(QueryExecutionId=query_execution_id)
            state = status["QueryExecution"]["Status"]["State"]
            if state in ["SUCCEEDED", "FAILED", "CANCELLED"]:
                break
            if elapsed >= self.MAX_POLL_SECONDS:
                athena.stop_query_execution(QueryExecutionId=query_execution_id)
                raise TimeoutError(
                    f"Athena query {query_execution_id} did not finish within "
                    f"{self.MAX_POLL_SECONDS}s — cancelled."
                )
            time.sleep(self.POLL_INTERVAL_SECONDS)
            elapsed += self.POLL_INTERVAL_SECONDS

        if state != "SUCCEEDED":
            reason = status["QueryExecution"]["Status"].get("StateChangeReason", "No details")
            raise Exception(f"Query failed: {state} — {reason}")

        col_names = None
        buffer = []
        next_token = None
        while True:
            kwargs = {"QueryExecutionId": query_execution_id}
            if next_token:
                kwargs["NextToken"] = next_token
            page = athena.get_query_results(**kwargs)

            page_rows = page["ResultSet"]["Rows"]
            if col_names is None:
                column_info = page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
                col_names = [col["Name"] for col in column_info]
                page_rows = page_rows[1:]

            for row in page_rows:
                buffer.append([c.get("VarCharValue", None) for c in row["Data"]])

            if len(buffer) >= chunksize:
                yield pd.DataFrame(buffer, columns=col_names)
                buffer = []

            next_token = page.get("NextToken")
            if not next_token:
                break

        if buffer:
            yield pd.DataFrame(buffer, columns=col_names)