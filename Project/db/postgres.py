from db.base import Database
import psycopg2
import pandas as pd


class Postgres(Database):
    def __init__(self, dbname, user, password, host, port, schema=""):
        self.dbname = dbname
        self.user = user
        self.password = password
        self.host = host
        self.port = port
        self.schema = schema

    def connect(self):
        options = f"-csearch_path={self.schema}" if self.schema else ""
        conn = psycopg2.connect(
            dbname=self.dbname,
            user=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            options=options,
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