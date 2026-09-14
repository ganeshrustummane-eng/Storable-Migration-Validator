from db.base import Database
import pandas as pd
import pyodbc


class Mssqlserver(Database):
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
        return pyodbc.connect(conn_str)

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
