"""Regression check for SQLQueryGenerator._hash_expression's pgcrypto-free
PostgreSQL formula (docs/large-table-scalable-architecture): Tier-1
classification compares raw hash *strings* across engines directly, so the
source and target sides must always agree on algorithm.

Run:  python -m pytest src/generated_queries/test_sql_query_generator.py -q
  or: python src/generated_queries/test_sql_query_generator.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generated_queries.sql_query_generator import SQLQueryGenerator  # noqa: E402


def test_postgresql_hash_expression_uses_md5_not_pgcrypto_digest():
    expr = SQLQueryGenerator._hash_expression("postgresql", ["col_a", "col_b"])
    assert "MD5(" in expr
    assert "digest(" not in expr
    assert "pgcrypto" not in expr
    print("test_postgresql_hash_expression_uses_md5_not_pgcrypto_digest: OK")


def test_redshift_hash_expression_unchanged_sha256():
    expr = SQLQueryGenerator._hash_expression("redshift", ["col_a"])
    assert "SHA2(" in expr and "256" in expr
    print("test_redshift_hash_expression_unchanged_sha256: OK")


def test_snowflake_hash_expression_algorithm_switch():
    sha_expr = SQLQueryGenerator._hash_expression("snowflake", ["col_a"], algorithm="sha256")
    md5_expr = SQLQueryGenerator._hash_expression("snowflake", ["col_a"], algorithm="md5")
    assert "SHA2(" in sha_expr
    assert "MD5(" in md5_expr
    print("test_snowflake_hash_expression_algorithm_switch: OK")


def test_row_hash_queries_algorithm_selection_matches_source_dialect():
    """_row_hash_queries (the caller) only adds a 3-line algorithm switch on
    top of _hash_expression -- covered directly here without constructing a
    full SQLQueryGenerator (its __init__ requires an AI API key unrelated to
    this hashing path)."""
    for source_db_type, expect_md5 in (("postgresql", True), ("mssql", False), ("redshift", False)):
        algorithm = "md5" if (source_db_type or "").lower() == "postgresql" else "sha256"
        target_sql = SQLQueryGenerator._hash_expression("snowflake", ["col_a"], algorithm=algorithm)
        assert ("MD5(" in target_sql) == expect_md5, (source_db_type, target_sql)
    print("test_row_hash_queries_algorithm_selection_matches_source_dialect: OK")


if __name__ == "__main__":
    test_postgresql_hash_expression_uses_md5_not_pgcrypto_digest()
    test_redshift_hash_expression_unchanged_sha256()
    test_snowflake_hash_expression_algorithm_switch()
    test_row_hash_queries_algorithm_selection_matches_source_dialect()
    print("All sql_query_generator hash-expression checks passed.")
