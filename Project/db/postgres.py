from db.base import Database
import psycopg2
import pandas as pd


class Postgres(Database):
    # ponytail: fixed defaults, not per-table configurable; raise on the
    # instance if one table's validation query genuinely needs longer.
    CONNECT_TIMEOUT_SECONDS = 10        # TCP/auth handshake only
    STATEMENT_TIMEOUT_SECONDS = 3600    # server-side query ceiling — 30 min

    def __init__(self, dbname, user, password, host, port, schema=""):
        self.dbname = dbname
        self.user = user
        self.password = password
        self.host = host
        self.port = port
        self.schema = schema

    def connect(self):
        # Multiple libpq startup options are space-separated within one
        # -c-prefixed string; statement_timeout is in milliseconds server-side.
        option_parts = [f"-c statement_timeout={self.STATEMENT_TIMEOUT_SECONDS * 1000}"]
        if self.schema:
            option_parts.append(f"-c search_path={self.schema}")
        conn = psycopg2.connect(
            dbname=self.dbname,
            user=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            options=" ".join(option_parts),
            connect_timeout=self.CONNECT_TIMEOUT_SECONDS,
        )
        return conn

    def execute_query(self, query):
        
        conn = self.connect()
        cur = None
        try:
            cur = conn.cursor()
            cur.execute(query)
            data = cur.fetchall()
            columns = [desc[0] for desc in cur.description] # type: ignore
            return pd.DataFrame(data, columns=columns)
        finally:
            if cur is not None:
                cur.close()
            conn.close()