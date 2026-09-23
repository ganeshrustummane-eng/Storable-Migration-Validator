# Graph Report - .  (2026-09-23)

## Corpus Check
- 210 files · ~211,814 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1909 nodes · 3472 edges · 107 communities (79 shown, 28 thin omitted)
- Extraction: 91% EXTRACTED · 9% INFERRED · 0% AMBIGUOUS · INFERRED: 297 edges (avg confidence: 0.63)
- Token cost: 102,899 input · 0 output

## Community Hubs (Navigation)
- Source Connection Discovery & Extractor Factory
- Agent/Skill Docs & CLAUDE.md Ground Rules
- Env Setup Wizard
- Athena Connector
- Rule Book Load/Persist
- Athena Schema Extractor
- AI SQL Query Generation
- Semantic Canonicalization Tests
- Column-Mapping Correction Feedback
- AI Prompt Builder (Ambiguous Columns)
- Tiered Runner (Hybrid Tier1/Tier2 Engine)
- Results Store & Runner
- Excel Batch Loader
- Candidate Matching & Confidence Scoring
- AI Rule Mapper (Claude/DIAL)
- Tiered Runner Edge-Case Tests
- Base Validation Rule Interface
- Config Schema Validation
- Generated Queries Package
- Column Rule Mapping & SQL Generation
- Semantic Normalize Core
- Webapp Source Table Discovery
- CanonicalValidationPlan Model
- Tiered Runner Fake-DB Tests
- Skip-Aware CLI Reporter
- Base Extractor Interface
- main.py Row Validation Core
- Batch Exclusion Report
- Quality Checks Aggregation
- Table Presence Checker
- Rule Compatibility Scoring
- Architecture Diagrams (Redshift Plan)
- PK-Indexed Row Comparison
- Candidate Matcher Orchestration
- Plan Validator & Rule Registry
- Validation Pipeline CLI Orchestrator
- JIRA Client
- Requirement Planner & Validation Plan
- Snowflake Discovery Cache
- Skip Analysis Report
- AI Call Token Logging
- Exclusion Report & Plan Store Package
- Plan Store Persistence
- Query Output Manager
- Bytea Rule
- Failed-Only CSV Dashboard
- NL Requirement Planner
- Redshift Source Support Plan
- Large-Table Scalability Design (Tier1/Tier2)
- Skip Classifier
- SQL Generation from Plan
- Model Probe (DIAL Availability)
- Fuzzy Column Matching
- AI Response Parser
- Table Presence Checker (impl)
- Custom SQL YAML Fix Plans
- Validation Docs (Rules/Strategies)
- Connector Skills Index
- Hybrid Dispatch Regression Tests
- Array Rule
- Boolean Rule
- Date Rule
- HStore Rule
- Integer Rule
- JSON Rule
- Numeric Rule
- Timestamp TZ Rule
- Timestamp NTZ Rule
- Text Rule
- UUID Rule
- Redshift Deps & Requirements
- Row Hash Expression Tests
- Token Usage Cost Report
- Custom SQL Plans & Exclusion Manager Removal
- Exclusion YAMLs & Database Registry
- Quality Checks Hybrid Tests
- Batch Table Mapping Store
- src/ Module Architecture Guide
- Base Rule Registry
- Logging Config
- Streamlit UI Bugfix ADRs (0005-0007)
- main.py Correctness Fixes History
- Bronze/Silver Postgres Configs
- Gold-Layer Configs (Fivetran-Filtered)
- Path Manager
- Run ID Generation
- Migration Filter History
- Graphify & Gemini-Cleanup ADRs (0008-0009)
- Rule Book Type Lookup
- CodeMie API/Coder Assistants
- CodeMie Debugger/Security Assistants
- Local Test Container Deps
- Sequence Diagrams (Stale Rule-Book Lifecycle)
- MSSQL Count Validation Config
- MSSQL Inventory Data Validation
- MSSQL Products Data Validation
- Test Path Conftest
- Expected Grain Default Test
- Null-Rate Divergence Test
- Validation Executor Utilities Init
- Connector Package Init (JIRA-only)
- Migration Validator Root Package
- CodeMie Documenter Assistant
- CodeMie Frontend Developer Assistant
- CodeMie Python Developer Assistant
- ADR Template

## God Nodes (most connected - your core abstractions)
1. `CanonicalValidationPlan` - 47 edges
2. `BaseValidationRule` - 43 edges
3. `ColumnMetadata` - 40 edges
4. `SQLQueryGenerator` - 36 edges
5. `AISQLQueryGenerator` - 34 edges
6. `ColumnRuleMapping` - 32 edges
7. `canonicalize_value()` - 31 edges
8. `_warn()` - 28 edges
9. `_head()` - 25 edges
10. `_dim()` - 25 edges

## Surprising Connections (you probably didn't know these)
- `CodeMie Code Reviewer virtual assistant` --semantically_similar_to--> `migration-validator-dqe-review agent`  [INFERRED] [semantically similar]
  .codemie/virtual_assistants/code_reviewer.yaml → .claude/agents/migration-validator-dqe-review.md
- `Project/readme.md (Data Validation Framework doc, older/stale)` --semantically_similar_to--> `README.md — Migration Validator overview`  [INFERRED] [semantically similar]
  Project/readme.md → README.md
- `Custom SQL tab YAML 'source:' field bug (writes src_0 instead of dialect name)` --semantically_similar_to--> `main.py multi-source config directory false positive`  [INFERRED] [semantically similar]
  plans/001-fix-custom-sql-yaml-source-field.md → docs/decisions/0007-false-positive-failure-multi-source-config-directory.md
- `Project/readme.md (Data Validation Framework doc, older/stale)` --conceptually_related_to--> `CanonicalValidationPlan`  [AMBIGUOUS]
  Project/readme.md → docs/api/schemas.md
- `Decision: run graphify skill to build persistent knowledge graph (AST + subagent semantic extraction) instead of manual grep/CLAUDE.md upkeep` --semantically_similar_to--> `Connector layer (src/connector/) — corrected: only jira_client.py is live, called directly from webapp/app.py, no agent/FastAPI/authz/audit`  [INFERRED] [semantically similar]
  docs/decisions/0008-graphify-knowledge-graph-for-codebase-navigation.md → CLAUDE.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Row-level and hybrid data comparison execution pipeline** — project_main_py, project_runner_py, project_tiered_runner_py, claude_skills_data_comparison_report_skill [EXTRACTED 1.00]
- **Learned-rules mutable feedback loop (gap-filler rules + human corrections)** — src_rule_book_py, src_learning_feedback_py, src_learning_retrieval_py, src_rule_book_learned_json [EXTRACTED 1.00]
- **Scattered Fivetran-column/exclusion detection across the codebase** — claude_skills_normalization_and_exclusions_skill, src_core_skip_classifier_py, src_rules_base_rules_py, claude_skills_connector_snowflake_target_skill [EXTRACTED 1.00]
- **Bronze/Silver/Gold layered validation configs for same table pairs (inventory, products, customers, orders)** — project_config_bronze_data_validation_mssql_inventory, project_config_silver_data_validation_mssql_inventory, project_config_bronze_data_validation_mssql_products [EXTRACTED 1.00]
- **Five near-duplicate exclusion YAMLs (global + 4 per-source-DB) sharing the same Fivetran metadata exclusion entries** — config_exclusions, config_athena_exclusions, config_mssql_exclusions, config_postgresql_exclusions, config_redshift_exclusions [EXTRACTED 1.00]
- **Large-table scalability trio: streaming/hash-narrowing decision, thread-pool parallelism decision, and the pipe-deadlock bug that parallelism's Stop-button feature introduced into runner.py** — docs_decisions_0001_pyspark_vs_streaming_chunking, docs_decisions_0002_table_level_thread_pool_parallelism, docs_decisions_0004_run_validation_hang_subprocess_pipe_deadlock [EXTRACTED 1.00]
- **hybrid_v1 scalable validation design (tiering, row_hash, canonicalization, grain parity)** — concept_tier1_tier2_hybrid_architecture, concept_row_hash_fallback, concept_semantic_normalize_canonicalization, concept_validate_expected_grain_hybrid_v1 [INFERRED 0.85]
- **Failed-only CSV surfacing chain from writer through glob to UI render/download** — project_main_failed_only_csv_writer, project_runner_failed_files_glob, webapp_app_render_diff_file, webapp_app_render_paginated_df [EXTRACTED 1.00]
- **Redshift source addition spanning connector, extractor, rules, exclusions, and UI** — project_db_factory_redshift_branch, sql_extractor_redshift_extractor, src_rules_redshift_rules_shim, config_redshift_exclusions_yaml, webapp_app_source_types_tuple [EXTRACTED 1.00]
- **Token usage logging instrumentation across both AI call sites into shared log/report** — src_ai_rule_planner, ai_sql_generator_call_site, token_logger, token_usage_jsonl_log, report_token_usage_cli [EXTRACTED 1.00]
- **Docs bannered with the same dated 'stale content notice' marking removed-chatbot-layer content as historical-only** — docs_architecture_system_architecture, docs_architecture_security_architecture, docs_deployment_jira_implementation_summary, docs_deployment_jira_ticket_resolver_enhancements, docs_deployment_jira_integration, docs_architecture_data_flow, stale_content_notice_pattern [EXTRACTED 1.00]
- **Graphify-driven cleanup flow: ADR 0008 builds the graph, ADR 0009 acts on findings surfaced by it, CLAUDE.md's connector-layer bullet is the concrete correction made** — docs_decisions_0008_graphify_knowledge_graph_for_codebase_navigation, docs_decisions_0009_remove_github_mirror_and_fix_stale_gemini_docs, claude_md_connector_layer, removed_chatbot_agent_layer [EXTRACTED 1.00]

## Communities (107 total, 28 thin omitted)

### Community 0 - "Source Connection Discovery & Extractor Factory"
Cohesion: 0.05
Nodes (114): print_connection_registry(), Read SRC_N_* and SOURCE_* vars from .env and return a list of connection dicts…, ExtractorFactory, Instantiates the correct BaseExtractor subclass from a db_type string., _apply_database_registry(), _banner(), _blank(), _build_parser() (+106 more)

### Community 1 - "Agent/Skill Docs & CLAUDE.md Ground Rules"
Cohesion: 0.05
Nodes (69): ApprovalRecord, AuditRecord, CanonicalValidationPlan, migration-validator-dqe-review agent, migration-validator-orchestrator agent, streamlit-frontend-manager agent, validation-query-yaml-generator agent, CLAUDE.md project instructions (+61 more)

### Community 2 - "Env Setup Wizard"
Cohesion: 0.07
Nodes (62): _blank(), _box(), _build_env_content(), _C, _collect_ai_settings(), _collect_one_source(), _collect_snowflake_target(), _dim() (+54 more)

### Community 3 - "Athena Connector"
Cohesion: 0.06
Nodes (27): Athena, Same get_query_results 1000-row/page pagination as execute_query — the…, Database, ABC, Any, Yield the result in bounded-size pandas DataFrame chunks instead of…, _find_source(), _load_env() (+19 more)

### Community 4 - "Rule Book Load/Persist"
Cohesion: 0.05
Nodes (33): _build_when_text(), Any, ValueError, Efficient Rule Book — PostgreSQL → Snowflake Validation Rules Manager…, Metadata record for one transformation rule. SQL generation is handled by the…, Format this rule for injection into an AI prompt., Serialize to dict for JSON persistence., Central manager for all transformation rules. Architecture ------------ Base… (+25 more)

### Community 5 - "Athena Schema Extractor"
Cohesion: 0.06
Nodes (16): AthenaExtractor, ExtractionError, MSSQLExtractor, _normalise_athena_type(), PostgresExtractor, PrimaryKeyInfo, Exception, Raised when schema extraction fails. (+8 more)

### Community 6 - "AI SQL Query Generation"
Cohesion: 0.07
Nodes (28): AIGeneratedQuery, AISQLGenerationError, AISQLQueryGenerator, RuntimeError, Render any user-taught rules (Rule Book UI tab → rule_book_learned.json) as an…, Build the system prompt with database-specific instructions., Args: api_key : DIAL API key (default: DIAL_API_KEY env var) api_base : DIAL…, Build the user prompt with column details including exact SQL expressions. (+20 more)

### Community 7 - "Semantic Canonicalization Tests"
Cohesion: 0.07
Nodes (44): canonicalize_value(), Return a canonical, comparable string for one cell. Non-semi-structured values…, Tests for the JSON/JSONB/HStore canonicalizer. Run: python -m pytest…, Defect 3: collation-dependent ordering no longer matters., Fractional values are rounded to 2dp — trailing-zero formatting differences…, Rounding to 2dp is a fixed precision floor: differences past the 2nd decimal…, Defect 4: jsonb_each() used to abort the query on these., Arrays are compared as sets, not sequences — a reordered array (e.g. a… (+36 more)

### Community 8 - "Column-Mapping Correction Feedback"
Cohesion: 0.07
Nodes (26): FeedbackRecorder, MismatchFeedback, prompt_for_feedback(), Path, Mismatch Feedback Workflow =========================== Records human…, Record a list of corrections. Returns the count of successful writes., Load the file, remove any existing entry for the same source+target, append the…, Load the current state of rule_book_learned.json (or return empty structure). (+18 more)

### Community 9 - "AI Prompt Builder (Ambiguous Columns)"
Cohesion: 0.07
Nodes (30): AI package — focused, token-efficient AI reasoning for ambiguous cases only.…, _filter_learned(), PromptBuilder, Prompt Builder =============== Builds minimal, focused prompts for AI-assisted…, Build the minimal user prompt for one ambiguous source column. Sends ONLY: -…, Return only learned examples relevant to this source column. Relevance criteria…, Builds focused, token-efficient prompts for ambiguous column resolution. Usage…, Build the system prompt that enforces all AI constraints. This prompt is sent… (+22 more)

### Community 10 - "Tiered Runner (Hybrid Tier1/Tier2 Engine)"
Cohesion: 0.08
Nodes (42): _append_result_batch(), _canonicalization_risk_columns(), _collect_hash_multimap(), _dialect_numeric_cast(), _dialect_text_cast(), _distinct_count_failures(), _empty_frame(), _fetch_batch() (+34 more)

### Community 11 - "Results Store & Runner"
Cohesion: 0.07
Nodes (34): Connection, fragment, _connect(), distinct_tables(), DataFrame, query_results(), query_runs(), SQLite-backed history for Project/main.py validation runs — replaces grepping… (+26 more)

### Community 12 - "Excel Batch Loader"
Cohesion: 0.08
Nodes (27): _apply_env(), _build_schema_context(), derive_row_plan(), _detect_source_type(), ExcelBatchLoader, _generate_queries(), _is_empty(), load_excel() (+19 more)

### Community 13 - "Candidate Matching & Confidence Scoring"
Cohesion: 0.08
Nodes (23): CandidateMatcher, Full deterministic matching pipeline. Usage ----- matcher = CandidateMatcher()…, ConfidenceBreakdown, ConfidenceScorer, _position_proximity(), Confidence Scorer ================== Computes an explainable multi-factor…, Return a score [0.0, 1.0] based on how close the ordinal positions are.…, Explainable breakdown of a confidence score for one column mapping. Attributes… (+15 more)

### Community 14 - "AI Rule Mapper (Claude/DIAL)"
Cohesion: 0.08
Nodes (25): AIRuleMapper, AIRuleMappingError, _is_claude_model(), RuntimeError, AI Rule Mapper =============== Uses DIAL/GPT-4o (or Claude direct) to…, Assigns validation rules to column pairs using DIAL or Claude direct. Backend…, Args: api_key : DIAL API key OR Claude API key (auto-detected from env if not…, # IMPORTANT: ignore any model= passed in if it looks like a DIAL/GPT model (+17 more)

### Community 15 - "Tiered Runner Edge-Case Tests"
Cohesion: 0.06
Nodes (23): _build_fixture(), _oracle_result(), Differential check for the hybrid Tier-1/Tier-2 engine: does tiering (accept…, grain_columns pointing at a column other than the hybrid key/PK can't be…, `{col}__nullish` (built from `col IS NULL OR CAST(col) = '<<NULL>>'`) already…, §R.2.2.3: COUNT(DISTINCT) excludes NULL; nunique(dropna=False) counts it as one…, §R.2.4.6: sample_hash_percent=100% of a million-row table must still only ever…, Base-rule-generated Snowflake targetqueries emit AS "id_normalized" (quoted… (+15 more)

### Community 16 - "Base Validation Rule Interface"
Cohesion: 0.08
Nodes (5): BaseValidationRule, NullPlaceholderRule, Abstract base for all validation rules. No source database is optional: every…, Bare NULL→'<<NULL>>' with plain text cast. Used when no other transformation…, Dispatch to the correct source-database dialect. Args: source_db_type:…

### Community 17 - "Config Schema Validation"
Cohesion: 0.10
Nodes (28): BaseModel, field_validator, ConfigValidationError, CountValidationBlock, _credential_exists(), DataValidationBlock, find_duplicate_table_keys(), is_validation_config() (+20 more)

### Community 18 - "Generated Queries Package"
Cohesion: 0.09
Nodes (24): Column Mapping =============== The ColumnRuleMapping dataclass — one…, Generated Queries Package =========================== Produces ready-to-run SQL…, Query Output Manager ===================== Orchestrates SQL generation + YAML…, SQL Query Generator ==================== Builds validation SQL queries for a…, _build_data_yaml(), _dump_yaml_document(), _LiteralStr, _load_yaml_document() (+16 more)

### Community 19 - "Column Rule Mapping & SQL Generation"
Cohesion: 0.15
Nodes (10): ColumnRuleMapping, A single source→target column pair with its assigned validation rule.…, Alias used on BOTH sides of the comparison. Always derived from the SOURCE…, Human-readable label for the source dialect used in SQL comments., Builds all validation SQL from a ColumnRuleMapping list. Multi-Database Support…, Generate all validation queries for the given table pair. Args: pg_schema :…, Return CTE prefix and relation for comparison-purpose joins., Return '\nWHERE ...' string, empty when nothing to filter. (+2 more)

### Community 20 - "Semantic Normalize Core"
Cohesion: 0.09
Nodes (29): canonicalize_frames(), _canonicalize_node(), _column_looks_semi_structured(), looks_semi_structured(), _normalize_numeric_str(), _parse_document(), parse_hstore_text(), Semi-structured value canonicalization (JSON / JSONB / HStore -> VARIANT)… (+21 more)

### Community 21 - "Webapp Source Table Discovery"
Cohesion: 0.09
Nodes (21): available_models_for_ui(), _build_row_hash_sql(), cached_source_tables(), connection_label(), flash(), _model_label(), pick_layer(), pick_source_location() (+13 more)

### Community 22 - "CanonicalValidationPlan Model"
Cohesion: 0.09
Nodes (13): CanonicalValidationPlan, ColumnMappingEntry, Describes how one source column maps to one target column. This is the core…, Serialize losslessly — this dict is the persisted contract., Single source of truth for one source→target table validation. Both SQL and…, Mappings that should be validated (skip_validation=False)., Mappings excluded from validation., Deterministically matched columns. (+5 more)

### Community 23 - "Tiered Runner Fake-DB Tests"
Cohesion: 0.08
Nodes (22): _FakeDB, PK-less tables never reach the quality-check region -- same documented…, Duplicate-PK scalability edge case, isolated from expected_grain (no grain…, PK-less scalability edge case: no pksourcecolumn/pktargetcolumn configured, so…, PK-less table where source has one more physically-identical row than target…, Phase 2 audit finding F2: row_hash.columns=[id, name] does not cover 'amount',…, Serves canned full-table data for a table's real query, and a pre-split…, Phase 2 audit finding F3: HASH_MATCH keys (7, all matching -- nothing else to… (+14 more)

### Community 24 - "Skip-Aware CLI Reporter"
Cohesion: 0.10
Nodes (14): _C, _NoColor, Skip-Aware CLI Reporter — Step 2 ===================================== Renders…, Dummy color class that returns empty strings for all attributes., Return a formatted single-line string for CLI output., Classifies each skipped column into a category and verdict. This is the single…, Classify a list of skipped column dicts. Each dict should have keys: 'column',…, Return True if any skip is UNJUSTIFIED (should cause FAIL). (+6 more)

### Community 25 - "Base Extractor Interface"
Cohesion: 0.08
Nodes (13): BaseExtractor, _normalise_mssql_type(), _normalise_redshift_type(), ABC, Universal Extractor — All Database Schema Extractors in One File…, Abstract interface all extractors implement., Return available schemas in the connected database. Override in subclasses., Return FK relationships for table as list of dicts: {fk_column, ref_schema,… (+5 more)

### Community 26 - "main.py Row Validation Core"
Cohesion: 0.14
Nodes (21): get_database(), Build a DB connector from .env credentials + optional YAML-level overrides.…, Best-effort ERROR summary row so a table that crashed mid-comparison still…, One table's full validations dict, run on a worker thread — see…, _validate_table(), _write_error_summary(), Self-check for the pure decision helpers in utility.py — no DB, no fixtures.…, test_count_validation_match() (+13 more)

### Community 27 - "Batch Exclusion Report"
Cohesion: 0.10
Nodes (10): BatchExclusionReport, ExcludedColumn, ExclusionReport, Any, Aggregates per-table coverage so a batch cannot hide a thin run., Recover the coverage report for an executing YAML block. The YAML is only a…, Column coverage for a single table validation., One-line coverage statement — print this next to every pass rate. (+2 more)

### Community 28 - "Quality Checks Aggregation"
Cohesion: 0.14
Nodes (21): _aggregate_value_failures(), append_validation_audit(), _as_float(), _column_pairs(), _hash_dataframe(), _pct_difference(), Any, DataFrame (+13 more)

### Community 29 - "Table Presence Checker"
Cohesion: 0.10
Nodes (10): Result of the full table presence check across all source tables. Attributes:…, Return (source_table, target_table) pairs for validated tables only., Render the full table presence report for CLI output. Shows: - MATCHED tables -…, Return a serialisable summary for logging and reporting., Perform the table presence check. Args: source_tables: List of table names from…, Convenience method to check a single source table against a target list. Args:…, Result for a single table in the presence check. Attributes: source_table:…, Return a single formatted line for CLI output. (+2 more)

### Community 30 - "Rule Compatibility Scoring"
Cohesion: 0.11
Nodes (10): Return a compatibility score [0.0, 1.0] for a PG→SF type pair. Uses normalized…, _type_compatibility(), Return the best matching validation rule for a (pg_type, sf_type) pair. Two-…, First ACTIVE learned rule (with reuses_rule set) matching this type pair, or…, _NoOpRule, _normalize_type(), ABC, PostgreSQL → Snowflake Transformation Rules… (+2 more)

### Community 31 - "Architecture Diagrams (Redshift Plan)"
Cohesion: 0.11
Nodes (21): AI Integration Flow diagram, Bi-Directional Column Analysis diagram, Database Connection Pattern diagram, High-Level System Architecture diagram, Validation Pipeline Flow diagram, Validation Result States diagram, config/database_registry.yaml SRC_4 (Redshift) entry, 2.3.2 AI Layer component (+13 more)

### Community 32 - "PK-Indexed Row Comparison"
Cohesion: 0.21
Nodes (19): _cell_str(), compare_indexed_frames(), PK-indexed row comparison core. Extracted verbatim from Project/main.py's…, Normalize a cell for comparison: - None/NaN → '<<NULL>>' - float/Decimal → 2-dp…, Compare two already-canonicalized frames by PK (scalar or composite/list).…, _row_key_str(), _to_df(), _by_key() (+11 more)

### Community 33 - "Candidate Matcher Orchestration"
Cohesion: 0.12
Nodes (14): _build_learned_lookup(), _has_learned_match(), Candidate Matcher ================== Orchestrates the full deterministic…, Run the full deterministic matching pipeline. Args: source_columns : PostgreSQL…, Build a set of (src_name_upper, tgt_name_upper) pairs from learned examples., Return True if (src_name, tgt_name) appears in the learned examples set., Exact Column Matcher ===================== Deterministic matching before any…, Fuzzy Column Matcher ===================== Uses RapidFuzz (or a pure-Python… (+6 more)

### Community 34 - "Plan Validator & Rule Registry"
Cohesion: 0.13
Nodes (15): get_registry(), Return the global rule registry for introspection or extension., Validation package — plan-level validation before SQL/YAML generation.…, _get_known_rule_ids(), PlanValidationError, PlanValidator, ValueError, Plan Validator ============== Validates a CanonicalValidationPlan before… (+7 more)

### Community 35 - "Validation Pipeline CLI Orchestrator"
Cohesion: 0.12
Nodes (14): _build_parser(), _default_reason(), _print_header_v2(), ArgumentParser, Path, Validation Pipeline — End-to-End Orchestrator…, Full end-to-end validation pipeline. Steps ----- 1. Extract live column…, Run the full new pipeline using the CanonicalValidationPlan architecture.… (+6 more)

### Community 36 - "JIRA Client"
Cohesion: 0.19
Nodes (18): add_comment(), create_ticket(), _get(), get_my_tickets(), get_ticket(), get_transitions(), is_configured(), JiraError (+10 more)

### Community 37 - "Requirement Planner & Validation Plan"
Cohesion: 0.17
Nodes (10): _apply_intent(), Any, Expected source-to-target expression comparison., Rebuild an entry from its ``to_dict()`` form., Rebuild a plan from its persisted JSON form., Metadata-backed relationship used by population or comparison validation., Normalized content fingerprint specification, separate from identity., RelationshipSpec (+2 more)

### Community 38 - "Snowflake Discovery Cache"
Cohesion: 0.14
Nodes (17): cache_data, _discover_snowflake_databases(), _discover_snowflake_schemas(), Return list of Snowflake databases the user can access., Return list of (schema_name, table_count) for a Snowflake database. Excludes…, cached_sf_column_types(), cached_sf_columns(), cached_sf_databases() (+9 more)

### Community 39 - "Skip Analysis Report"
Cohesion: 0.20
Nodes (8): Any, Renders the enhanced CLI validation report with skip visibility. Three output…, Analyse one table's validation result and classify all skips. Args: table_name:…, Print the complete three-section CLI report. Args: validation_results: Dict of…, Return (status_string, color_code) for the summary table., Skip analysis for one table. Produced by SkipAwareCLIReporter.analyse_table()…, SkipAwareCLIReporter, TableSkipReport

### Community 40 - "AI Call Token Logging"
Cohesion: 0.15
Nodes (13): extract_anthropic_usage(), extract_openai_usage(), log_usage(), AI-Powered SQL Query Generator ================================ Uses AI to…, Call EPAM DIAL via AzureOpenAI SDK., Call Anthropic Claude directly. Converts OpenAI-style messages list to…, extract_anthropic_usage(), extract_openai_usage() (+5 more)

### Community 41 - "Exclusion Report & Plan Store Package"
Cohesion: 0.18
Nodes (11): Exclusion Report ================= Makes excluded columns impossible to…, Core package — the CanonicalValidationPlan contract. The plan is the single…, Plan Store =========== Persistence for the CanonicalValidationPlan. The plan…, MatchMethod, PlanStatus, Enum, Canonical Validation Plan ========================== The…, One composable validation intent within a plan. (+3 more)

### Community 42 - "Plan Store Persistence"
Cohesion: 0.19
Nodes (10): PlanStore, PlanStoreError, Path, RuntimeError, Raised when a plan cannot be read, written, or parsed., Reads and writes CanonicalValidationPlan JSON files., Deterministic on-disk location for a table's plan., Write the plan atomically and return its path. (+2 more)

### Community 43 - "Query Output Manager"
Cohesion: 0.17
Nodes (11): GenerationResult, Path, QueryOutputManager, Orchestrates SQL generation + YAML file output for a single table pair.…, Generate all validation output for one table pair. Args: table_name : Display…, Generate all validation output from a CanonicalValidationPlan. Order matters:…, Result returned by QueryOutputManager.generate(). Attributes: table_name :…, Return a human-readable generation summary. (+3 more)

### Community 44 - "Bytea Rule"
Cohesion: 0.13
Nodes (7): ByteaRule, Binary/BYTEA: hex encoding for cross-system comparison. NULL→'<<NULL>>'., get_rule_by_name(), get_rule_for_type(), Rules Package — Source → Snowflake Validation Transformation Rules…, Look up the correct validation rule for a source→Snowflake type pair., Look up a registered base rule by its rule_name (e.g. 'text', 'boolean').

### Community 45 - "Failed-Only CSV Dashboard"
Cohesion: 0.16
Nodes (13): Plan 003: Surface failed-only CSVs (colored) on Run Validation dashboard, Project/main.py failed-rows-only CSV writer, Project/runner.py diff_files glob (*_result_*.csv), Project/runner.py failed_files glob (*_failed_*.csv) — new, create_summary(), Summary report CSV generation utilities, Create summary CSV for validation results Appends one row per table validation…, Colors a 'status' column (PASS/FAIL) green/red if present — purely cosmetic. (+5 more)

### Community 46 - "NL Requirement Planner"
Cohesion: 0.27
Nodes (13): build_plan_from_requirement(), build_planner_prompt(), _columns(), _parse_json_object(), Any, ValueError, Natural-language requirement to metadata-checked canonical plan intent. AI…, Raised when AI intent cannot be proven against schema metadata. (+5 more)

### Community 47 - "Redshift Source Support Plan"
Cohesion: 0.27
Nodes (13): Athena CTAS-to-S3 extraction strategy (bypasses 1000-row/page ceiling), Project/main.py as the correctness oracle / single live validation engine, 200-300M row scale performance audit bottlenecks (fetchall, PK loop, Athena pagination), PySpark cluster rejected as alternative to streaming/chunking, run_quality_checks (null_rate/distinct_count/aggregate/sample_hash) parity gap for hybrid_v1 (design-only, unresolved), row_hash fallback for PK-less tables, canonicalize_frames semantic normalization (JSON/hstore/decimal), Tier 1 (SQL hash pre-filter) / Tier 2 (exact Python compare) hybrid architecture (+5 more)

### Community 48 - "Large-Table Scalability Design (Tier1/Tier2)"
Cohesion: 0.18
Nodes (13): config/redshift_exclusions.yaml, Plan 004: Add AWS Redshift as a 4th source database type, setup_wizard.py discovery functions (_discover_postgres_databases, _discover_mssql_schemas), sql_extractor/extractors.py _REGISTRY extension pattern, RedshiftExtractor (subclasses PostgresExtractor), Normalization rules quick reference table (boolean/numeric/timestamp/date/text/uuid/integer/json/bytea/null), rule_book.py — evolving rule catalog manager, src/rules/base_rules.py — canonical rule logic file (+5 more)

### Community 49 - "Skip Classifier"
Cohesion: 0.19
Nodes (11): _contains_any(), _matches_any(), Enum, Skip Classifier — Step 1 =========================== Classifies every skipped…, Return True if text matches any of the regex patterns (case-insensitive)., Return True if text contains any of the phrases (case-insensitive)., Classify a single skipped column. Args: column_name: Original column name from…, Category assigned to a skipped column. (+3 more)

### Community 50 - "SQL Generation from Plan"
Cohesion: 0.19
Nodes (8): _plan_to_rule_mappings(), Convert CanonicalValidationPlan active_mappings to ColumnRuleMapping list. This…, Generate all validation queries from a CanonicalValidationPlan. This is the new…, Compose explicit filters with safe, symmetric population relationships., All SQL queries generated for one source → target table pair. Attributes:…, ValidationQuerySet, Write the data validation YAML for a single table. Output:…, Write the YAML config file directly from a CanonicalValidationPlan. This is the…

### Community 51 - "Model Probe (DIAL Availability)"
Cohesion: 0.21
Nodes (12): get_working_models(), invalidate_cache(), load_cache(), probe_models(), Model Probe =========== Tests which DIAL models are actually reachable with the…, Return only the models from `models` that respond successfully. Uses a 24-hour…, Delete the cache file so the next call re-probes., Return (model, ok, error_or_None). (+4 more)

### Community 52 - "Fuzzy Column Matching"
Cohesion: 0.17
Nodes (5): FuzzyMatchGroup, Compute normalized similarity score between two strings. Uses…, Score a specific source-target column pair. Args: source_col : Source column…, For every unmatched source column, rank all unmatched target columns by fuzzy…, All ranked candidates for one source column. Attributes: source_col : The…

### Community 53 - "AI Response Parser"
Cohesion: 0.18
Nodes (7): AIColumnDecision, _find_original_case(), Parse and validate an AI response. Args: raw_json : Raw string from the AI…, Remove markdown code fences if the AI wrapped its response., Return the original-case version of name from candidates (case-insensitive)., Validated AI response for one column mapping decision. Attributes:…, _strip_markdown()

### Community 54 - "Table Presence Checker (impl)"
Cohesion: 0.22
Nodes (8): Enum, Table Presence Checker — Step 3 =================================== Checks…, Checks whether every source table exists in the target before validation.…, Args: exclusions_config_path: Path to config/exclusions.yaml. Used to check if…, Load table-level exclusions from config/exclusions.yaml. Expected YAML format…, Presence status for a single source table., TablePresenceChecker, TableStatus

### Community 55 - "Custom SQL YAML Fix Plans"
Cohesion: 0.22
Nodes (10): src/generated_queries/ai_sql_generator.py — sql_generation AI call site, AISQLQueryGenerator.generate_custom_query(), src/profiling/ai_recommendation.py (unwired, not covered by token logging), report_token_usage.py — CLI token/cost report, token_logger.py — shared logger, extracts usage from OpenAI/Anthropic responses, token_usage_analysis/README.md — token usage & cost analysis, logs/token_usage.jsonl — append-only AI call log, pricing.json — per-model $/1M token rates (+2 more)

### Community 56 - "Validation Docs (Rules/Strategies)"
Cohesion: 0.22
Nodes (10): Per-dialect type normalization table (boolean/timestamp/integer/json/uuid), <<NULL>> COALESCE sentinel convention, Rule Book approval-role/VersionStore lifecycle (aspirational/stale — doesn't match src/rule_book.py), STATIC_EXCLUDE_COLUMNS / Fivetran pattern exclusions, Count Validation vs Data Validation strategies, Rule Book (docs), Rule Examples, Normalization Rules (+2 more)

### Community 57 - "Connector Skills Index"
Cohesion: 0.28
Nodes (9): connector-athena skill, connector-mssql-sitelink skill, connector-postgresql skill, connector-redshift-tradeshift skill, Project/db/athena.py, Project/db/factory.py, Project/db/mssqlserver.py, Project/db/postgres.py (+1 more)

### Community 58 - "Hybrid Dispatch Regression Tests"
Cohesion: 0.33
Nodes (8): _dispatch_decisions(), Regression test for Phase 2 audit finding F1: row_hash_validation (and any…, Mirrors main.py's exact per-block loop: for every validation_name in the…, Every existing table today (no execution_strategy configured) must keep falling…, test_no_dispatch_at_all_when_execution_strategy_not_set(), test_only_data_validation_dispatches_to_hybrid(), test_row_hash_validation_never_dispatches_as_its_own_validation(), test_sibling_helper_blocks_never_dispatch()

### Community 70 - "Redshift Deps & Requirements"
Cohesion: 0.29
Nodes (8): apscheduler (scheduled validation runs), psycopg2-binary (PostgreSQL/Redshift driver), pyathena (Athena column extraction), pyodbc (MSSQL driver), rapidfuzz (fuzzy matching, graceful fallback to difflib), snowflake-connector-python, Project/db/factory.py redshift branch (reuses Postgres class), requirements.txt — Python dependencies

### Community 71 - "Row Hash Expression Tests"
Cohesion: 0.36
Nodes (6): Regression check for SQLQueryGenerator._hash_expression's pgcrypto-free…, _row_hash_queries (the caller) only adds a 3-line algorithm switch on top of…, test_postgresql_hash_expression_uses_md5_not_pgcrypto_digest(), test_redshift_hash_expression_unchanged_sha256(), test_row_hash_queries_algorithm_selection_matches_source_dialect(), test_snowflake_hash_expression_algorithm_switch()

### Community 72 - "Token Usage Cost Report"
Cohesion: 0.39
Nodes (7): _cost_for(), _load_pricing(), _load_records(), main(), Token Usage & Cost Report =========================== Reads…, _summarize(), _u_totals()

### Community 73 - "Custom SQL Plans & Exclusion Manager Removal"
Cohesion: 0.33
Nodes (7): Custom SQL tab YAML 'source:' field bug (writes src_0 instead of dialect name), Plan 001 — Fix YAML source: field in Custom SQL Validation tab, Plan 002: Add AI-generated Snowflake SQL in Custom SQL tab, plans/README.md — execution order & audit log, config_schema.py SUPPORTED_SOURCES allowlist, Deleted src/exclusions/ (exclusion_manager + bidirectional_exclusion_handler), webapp/app.py Custom SQL tab source: field bug (src_0 vs dialect)

### Community 74 - "Exclusion YAMLs & Database Registry"
Cohesion: 0.43
Nodes (3): config/database_registry.yaml (non-secret connection metadata), config/exclusions.yaml (global exclusions), Project/notes.txt (TODO/testing notes)

### Community 75 - "Quality Checks Hybrid Tests"
Cohesion: 0.29
Nodes (7): _quality_fixture(), Both sides HASH_MATCH on every key (row-level comparison alone would PASS) --…, source's payload is the same JSON text on both rows (raw distinct=1); target's…, test_quality_checks_hybrid_aggregate_sum_drift_end_to_end(), test_quality_checks_hybrid_distinct_count_skips_canonicalization_risk_column(), test_quality_checks_hybrid_empty_tables_no_crash(), test_quality_checks_hybrid_null_rate_drift_end_to_end()

### Community 76 - "Batch Table Mapping Store"
Cohesion: 0.52
Nodes (6): ensure_table(), _get_connection(), load_confirmed_mappings(), Batch Table Mapping Store ========================= Persists confirmed source-…, Latest confirmed target table per source table, or {} on any failure (missing…, save_mapping()

### Community 77 - "src/ Module Architecture Guide"
Cohesion: 0.43
Nodes (7): src/README.md — Modular Architecture Guide, AI model selection (DIAL_MODEL, gpt-4o/gpt-4o-mini/etc), src/ai_transformation/ — column mapping + rule assignment (legacy, orchestrator removed per CLAUDE.md), src/generated_queries/ — SQL + YAML output generation, src/rules/ — type-specific normalization rules module, src/sql_extractor/ — live schema extraction module, validate_cli.py — interactive CLI main entry point

### Community 78 - "Base Rule Registry"
Cohesion: 0.29
Nodes (3): Maps (pg_type, sf_type) pairs to validation rules — first match wins., Look up a registered base rule by its rule_name (e.g. 'text', 'boolean')., RuleRegistry

### Community 79 - "Logging Config"
Cohesion: 0.33
Nodes (6): add_file_handler(), get_logger(), Logger, Logging configuration utilities, Create logger with console handler Args: name: Logger name (usually __name__)…, Add file handler to existing logger Args: logger: Logger instance…

### Community 80 - "Streamlit UI Bugfix ADRs (0005-0007)"
Cohesion: 0.47
Nodes (6): main.py multi-source config directory false positive, Run Validation results-render misindentation bug, Streamlit @st.fragment polling fix, ADR 0005: Run Validation slow — full-page rerun polling, ADR 0006: Run Validation results never rendered — misindented block, ADR 0007: False-positive failure — multi-source config directory

### Community 81 - "main.py Correctness Fixes History"
Cohesion: 0.50
Nodes (5): Point-in-time source/target consistency (unresolved, needs design), Session 4: Project/main.py correctness fixes (8 findings), Project/utils/test_utility_checks.py self-check test, utility.py count_validation_match() + count_mismatch_threshold_pct, row_hash_fallback_looks_like_column_drift() heuristic

### Community 82 - "Bronze/Silver Postgres Configs"
Cohesion: 0.50
Nodes (4): Bronze count_validation config (Postgres: customers, orders), Bronze data_validation config: customers (Postgres -> Snowflake), Bronze data_validation config: orders (Postgres -> Snowflake), Silver count_validation config (Postgres: customers, orders)

### Community 83 - "Gold-Layer Configs (Fivetran-Filtered)"
Cohesion: 0.50
Nodes (4): Gold count_validation config (MSSQL: employees_migration_test), Gold count_validation config (Postgres: semistructured_demo), Gold data_validation config: employees_migration_test (MSSQL SiteLink -> Snowflake, Fivetran-active filtered), Gold data_validation config: semistructured_demo (Postgres semi-structured types -> Snowflake VARIANT)

### Community 84 - "Path Manager"
Cohesion: 0.50
Nodes (3): get_config_output_paths(), Path management utilities for validation runs, Build directory structure and path mappings for validation execution Args:…

### Community 85 - "Run ID Generation"
Cohesion: 0.50
Nodes (3): generate_runid(), Run ID and timestamp generation utilities, Generate unique run IDs for batch validation tracking Returns: tuple:…

### Community 86 - "Migration Filter History"
Cohesion: 0.50
Nodes (4): filter_options_for(), load_filter_history(), Scan all persisted plan JSON files and return previously-used migration filters…, Return previously-used (source_filter, target_filter) pairs for a table. Empty…

### Community 87 - "Graphify & Gemini-Cleanup ADRs (0008-0009)"
Cohesion: 1.00
Nodes (3): ADR 0008: Use graphify to build a navigable knowledge graph, ADR 0009: Remove .github mirror; fix stale chatbot/Gemini docs, docs/decisions/README.md — Decision Log index

### Community 88 - "Rule Book Type Lookup"
Cohesion: 0.67
Nodes (3): get_rule_for_type_specific(), Same as get_rule_for_type(), but returns None instead of falling back to the…, _covered()

## Ambiguous Edges - Review These
- `src/rules/rules_catalog.json` → `src/rules/base_rules.py`  [AMBIGUOUS]
  .claude/skills/base-rules-datatypes/SKILL.md · relation: shares_data_with
- `Project/readme.md (Data Validation Framework doc, older/stale)` → `CanonicalValidationPlan`  [AMBIGUOUS]
  Project/readme.md · relation: conceptually_related_to
- `config/exclusions.yaml (global exclusions)` → `config/redshift_exclusions.yaml`  [AMBIGUOUS]
  config/redshift_exclusions.yaml · relation: shares_data_with
- `Project/db/factory.py redshift branch (reuses Postgres class)` → `Project/db/factory.py redshift branch (reuses Postgres class)`  [AMBIGUOUS]
  plans/004-add-redshift-source-support.md · relation: references

## Knowledge Gaps
- **58 isolated node(s):** `_C`, `Project/db/athena.py`, `Project/db/mssqlserver.py`, `Project/db/snowflake.py`, `src/core/skip_classifier.py` (+53 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **28 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `src/rules/rules_catalog.json` and `src/rules/base_rules.py`?**
  _Edge tagged AMBIGUOUS (relation: shares_data_with) - confidence is low._
- **What is the exact relationship between `Project/readme.md (Data Validation Framework doc, older/stale)` and `CanonicalValidationPlan`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `config/exclusions.yaml (global exclusions)` and `config/redshift_exclusions.yaml`?**
  _Edge tagged AMBIGUOUS (relation: shares_data_with) - confidence is low._
- **What is the exact relationship between `Project/db/factory.py redshift branch (reuses Postgres class)` and `Project/db/factory.py redshift branch (reuses Postgres class)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **Why does `CanonicalValidationPlan` connect `CanonicalValidationPlan Model` to `Plan Validator & Rule Registry`, `Validation Pipeline CLI Orchestrator`, `Requirement Planner & Validation Plan`, `Skip Analysis Report`, `Exclusion Report & Plan Store Package`, `Plan Store Persistence`, `Query Output Manager`, `NL Requirement Planner`, `SQL Generation from Plan`, `Column Rule Mapping & SQL Generation`, `Generated Queries Package`, `Skip-Aware CLI Reporter`, `Batch Exclusion Report`?**
  _High betweenness centrality (0.114) - this node is a cross-community bridge._
- **Why does `ValidationPipeline` connect `Validation Pipeline CLI Orchestrator` to `Source Connection Discovery & Extractor Factory`, `Plan Validator & Rule Registry`, `Rule Book Load/Persist`, `Column-Mapping Correction Feedback`, `AI Prompt Builder (Ambiguous Columns)`, `Exclusion Report & Plan Store Package`, `Query Output Manager`, `Excel Batch Loader`, `Candidate Matching & Confidence Scoring`, `CanonicalValidationPlan Model`?**
  _High betweenness centrality (0.105) - this node is a cross-community bridge._
- **Why does `ConnectionProfileManager` connect `Excel Batch Loader` to `Source Connection Discovery & Extractor Factory`, `Env Setup Wizard`, `Validation Pipeline CLI Orchestrator`, `Rule Book Load/Persist`, `Plan Store Persistence`?**
  _High betweenness centrality (0.091) - this node is a cross-community bridge._