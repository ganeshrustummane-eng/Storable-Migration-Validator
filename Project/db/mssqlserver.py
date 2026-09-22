from db.base import Database
import pandas as pd
import pyodbc


class Mssqlserver(Database):
    # ponytail: fixed defaults, not per-table configurable; raise on the
    # instance if one table's validation query genuinely needs longer.
    CONNECT_TIMEOUT_SECONDS = 10        # login handshake only
    QUERY_TIMEOUT_SECONDS = 3600        # server-side query ceiling — 30 min

    def __init__(self, DRIVER, SERVER, DATABASE, UID, PWD):
        self.DRIVER = DRIVER
        self.SERVER = SERVER
        self.DATABASE = DATABASE
        self.UID = UID
        self.PWD = PWD

    def connect(self):
        if self.UID and self.PWD:
            # SQL Server authentication (username + password)
            conn_str = (
                f"DRIVER={{{self.DRIVER}}};"
                f"SERVER={self.SERVER};"
                f"DATABASE={self.DATABASE};"
                f"UID={self.UID};"
                f"PWD={self.PWD};"
                "TrustServerCertificate=yes;"
            )
        else:
            # Windows / Azure AD integrated authentication
            conn_str = (
                f"DRIVER={{{self.DRIVER}}};"
                f"SERVER={self.SERVER};"
                f"DATABASE={self.DATABASE};"
                "Trusted_Connection=yes;"
                "Encrypt=yes;"
                "TrustServerCertificate=yes;"
            )
        conn = pyodbc.connect(conn_str, timeout=self.CONNECT_TIMEOUT_SECONDS)
        # Connection.timeout is pyodbc's query-timeout (SQL_ATTR_QUERY_TIMEOUT),
        # applied to every statement executed through this connection — the
        # connect() timeout= kwarg above only covers the login handshake.
        conn.timeout = self.QUERY_TIMEOUT_SECONDS
        return conn

    def execute_query(self, query):
        conn = self.connect()
        cur = None
        try:
            cur = conn.cursor()
            cur.execute(query)
            columns = [col[0] for col in cur.description]
            rows = cur.fetchall()
            return pd.DataFrame.from_records(rows, columns=columns)
        finally:
            if cur is not None:
                cur.close()
            conn.close()

    def execute_query_stream(self, query, chunksize=50_000):
        conn = self.connect()
        cur = None
        try:
            cur = conn.cursor()
            cur.execute(query)
            columns = [col[0] for col in cur.description]
            while True:
                batch = cur.fetchmany(chunksize)
                if not batch:
                    break
                yield pd.DataFrame.from_records(batch, columns=columns)
        finally:
            if cur is not None:
                cur.close()
            conn.close()
