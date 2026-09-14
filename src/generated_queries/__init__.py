"""
Generated Queries Package
===========================
Produces ready-to-run SQL + companion YAML validation config files
from a list of ColumnRuleMappings.

Modules
-------
  sql_query_generator  — Builds all PostgreSQL + Snowflake validation SQL
                         (no PK dependency — PKs deferred to future milestone)
  yaml_config_writer   — Writes YAML config in the project-standard format:
                             tables:
                               <table>:
                                 validations:
                                   data_validation:
                                     source_table_name: ...
                                     source: postgresql
                                     pksourcecolumn: <first_col>
                                     sourcequery: |
                                       SELECT ...
                                     target_table_name: ...
                                     target: snowflake
                                     pktargetcolumn: <first_col>  # source-derived alias
                                     targetquery: |
                                       SELECT ...
  query_output_manager — Orchestrates SQL gen + YAML write + file saves

Usage
-----
    from generated_queries import QueryOutputManager

    manager = QueryOutputManager()
    result = manager.generate(
        table_name="events",
        pg_schema="public",
        pg_table="events",
        sf_database="dev_edge_bronze",
        sf_schema="storedge_fms_public",
        sf_table="EVENTS",
        mappings=column_rule_mappings,
        has_fivetran_active=True,
        generated_by="AI",
        model_used="gpt-4o",
    )
    print(result.yaml_path)  # Path to saved .yaml file
    print(result.summary())  # Human-readable summary
"""

from generated_queries.sql_query_generator import SQLQueryGenerator, ValidationQuerySet
from generated_queries.yaml_config_writer import YAMLConfigWriter
from generated_queries.query_output_manager import QueryOutputManager, GenerationResult

__all__ = [
    "SQLQueryGenerator",
    "ValidationQuerySet",
    "YAMLConfigWriter",
    "QueryOutputManager",
    "GenerationResult",
]
