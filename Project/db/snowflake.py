from db.base import Database
import snowflake.connector
import pandas as pd


class Snowflake(Database):
    # ponytail: fixed defaults, not per-table configurable; raise on the
    # instance if one table's validation query genuinely needs longer.
    # Verified against installed snowflake-connector-python==4.7.1
    # (DEFAULT_CONFIGURATION exposes login_timeout/network_timeout/
    # socket_timeout/session_parameters as real connect() kwargs).
    LOGIN_TIMEOUT_SECONDS = 30
    NETWORK_TIMEOUT_SECONDS = 3600       # bounds all post-login network I/O
    STATEMENT_TIMEOUT_SECONDS = 3600     # server-side query ceiling — 30 min

    def __init__(self, SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD,
                 SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA, SNOWFLAKE_WAREHOUSE=""):
        self.SNOWFLAKE_ACCOUNT = SNOWFLAKE_ACCOUNT
        self.SNOWFLAKE_USER = SNOWFLAKE_USER
        self.SNOWFLAKE_PASSWORD = SNOWFLAKE_PASSWORD
        self.SNOWFLAKE_DATABASE = SNOWFLAKE_DATABASE
        self.SNOWFLAKE_SCHEMA = SNOWFLAKE_SCHEMA
        self.SNOWFLAKE_WAREHOUSE = SNOWFLAKE_WAREHOUSE

    def connect(self):
        kwargs = dict(
            account=self.SNOWFLAKE_ACCOUNT,
            user=self.SNOWFLAKE_USER,
            password=self.SNOWFLAKE_PASSWORD,
            database=self.SNOWFLAKE_DATABASE,
            schema=self.SNOWFLAKE_SCHEMA,
            login_timeout=self.LOGIN_TIMEOUT_SECONDS,
            network_timeout=self.NETWORK_TIMEOUT_SECONDS,
            session_parameters={
                "STATEMENT_TIMEOUT_IN_SECONDS": self.STATEMENT_TIMEOUT_SECONDS,
            },
        )
        if self.SNOWFLAKE_WAREHOUSE:
            kwargs["warehouse"] = self.SNOWFLAKE_WAREHOUSE
        return snowflake.connector.connect(**kwargs)

    def execute_query(self, query):
        with self.connect() as conn:
            with conn.cursor() as cs:
                if self.SNOWFLAKE_WAREHOUSE:
                    cs.execute(f"USE WAREHOUSE {self.SNOWFLAKE_WAREHOUSE};")
                cs.execute(query)
                rows = cs.fetchall()
                df = pd.DataFrame(rows, columns=[c[0] for c in cs.description])
                return df
