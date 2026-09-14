"""
╔══════════════════════════════════════════════════════════════════════╗
║   Migration Validator CLI  —  Multi-DB → Snowflake                  ║
║   Interactive + command-line interface                               ║
╚══════════════════════════════════════════════════════════════════════╝

Commands
--------
  setup         First-run wizard: configure databases + AI (.env setup)
  generate      Single table: extract schema → assign rules → SQL + YAML
  multi         Multi-table: pick DB → schema → tables → generate all
  batch         Multiple tables: read tables.yaml → run all in parallel
  rules         Show the full rule book (base + learned)
  add-rule      Add a new rule to the evolving rule book
  list-models   Show all available AI models
  list-tables   List tables in all configured databases
  profiles      Manage saved connection profiles
  help / (none) Interactive menu

Usage
-----
  cd src
  python validate_cli.py                              ← interactive menu
  python validate_cli.py setup                        ← first-run wizard
  python validate_cli.py generate \\
      --source-table events --sf-table EVENTS         ← single table
  python validate_cli.py generate \\
      --source-table events --sf-table EVENTS \\
      --model gpt-4o-mini                             ← choose model
  python validate_cli.py generate \\
      --connection-profile fms-dev                    ← use saved profile
  python validate_cli.py multi \\
      --connection-profile fms-dev \\
      --tables events,users,orders                    ← profile + tables
  python validate_cli.py batch --config tables.yaml   ← multiple tables
  python validate_cli.py batch --config tables.yaml --dry-run
  python validate_cli.py profiles                     ← list profiles
  python validate_cli.py profiles delete fms-dev      ← delete profile
  python validate_cli.py rules                        ← view rule book
  python validate_cli.py list-models                  ← list AI models
  python validate_cli.py add-rule                     ← add custom rule
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

# ── Ensure src/ and the project root are in path ─────────────────────────────
# Project root is needed so token_usage_analysis/ (real AI token/cost logging,
# imported from src/ai/rule_planner.py and src/generated_queries/ai_sql_generator.py)
# can be imported regardless of the current working directory.
_SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(_SRC_DIR))
sys.path.insert(0, str(_SRC_DIR.parent))

# Single shared implementation of JSON/JSONB/HStore canonicalization, also used
# by Project/main.py (the runtime comparison engine). Resolved via the project
# root added to sys.path just above.
from Project.utils.semantic_normalize import canonicalize_value  # noqa: E402

# Windows consoles default to cp1252, which cannot encode the box-drawing and
# arrow characters used throughout this CLI — including inside argparse help,
# so even `--help` would crash. Force UTF-8 on every stream we write to.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ── Load .env ─────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(_SRC_DIR.parent / ".env")
except ImportError:
    pass


# ─────────────────────────────────────────────────────────────────────────────
# ANSI colour helpers
# ─────────────────────────────────────────────────────────────────────────────

class _C:
    BOLD    = "\033[1m"
    GREEN   = "\033[92m"
    CYAN    = "\033[96m"
    BLUE    = "\033[94m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    MAGENTA = "\033[95m"
    DIM     = "\033[2m"
    RESET   = "\033[0m"


def _ok(msg):   print(f"{_C.GREEN}  ✓ {msg}{_C.RESET}")
def _warn(msg): print(f"{_C.YELLOW}  ⚠ {msg}{_C.RESET}")
def _err(msg):  print(f"{_C.RED}  ✗ {msg}{_C.RESET}")
def _head(msg): print(f"\n{_C.BOLD}{_C.CYAN}{msg}{_C.RESET}")
def _dim(msg):  print(f"{_C.DIM}  {msg}{_C.RESET}")
def _sep(char="─", width=62): print(f"  {char * width}")


def _banner():
    print(f"""
{_C.CYAN}{_C.BOLD}
╔══════════════════════════════════════════════════════════════════╗
║      Migration Validator  —  AI Query Generator                 ║
║      PostgreSQL / MSSQL / Athena  →  Snowflake  Completeness   ║
╚══════════════════════════════════════════════════════════════════╝{_C.RESET}""")


def _prompt(label: str, default: str = "") -> str:
    """Prompt the user with an optional default value."""
    if default:
        display = f"  {_C.BOLD}{label}{_C.RESET} [{_C.DIM}{default}{_C.RESET}]: "
    else:
        display = f"  {_C.BOLD}{label}{_C.RESET}: "
    value = input(display).strip()
    return value if value else default


# ─────────────────────────────────────────────────────────────────────────────
# DB TYPE NORMALIZATION
# ─────────────────────────────────────────────────────────────────────────────

_DB_TYPE_NORMALIZE = {
    "postgresql": "postgresql", "mssql": "mssql",
    "snowflake":  "snowflake",  "athena": "athena",
    "postgres": "postgresql", "pg": "postgresql",
    "sqlserver": "mssql", "sql_server": "mssql",
    "mssqlserver": "mssql", "microsoftsqlserver": "mssql",
    "aws_athena": "athena", "aws athena": "athena",
    "redshift": "redshift", "aws_redshift": "redshift", "aws redshift": "redshift",
}

_DB_TYPE_LABELS = {
    "postgresql": "PostgreSQL",
    "mssql":      "MS SQL Server",
    "snowflake":  "Snowflake (source)",
    "athena":     "AWS Athena",
    "redshift":   "AWS Redshift",
}


def _normalize_db_type(raw: str) -> str:
    """Map any env-stored db_type alias to a canonical ExtractorFactory key."""
    return _DB_TYPE_NORMALIZE.get(raw.lower().strip(), raw.lower().strip())


# ─────────────────────────────────────────────────────────────────────────────
# CONNECTION PROFILE MANAGER
# Profiles live in ~/.migration-validator/profiles.json (outside the repo).
# Each profile stores one (source + snowflake target) connection set so the
# user can skip re-entering credentials on every run.
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# STATIC COLUMN EXCLUSION LIST
# These columns are always excluded from validation regardless of user input.
# Fivetran audit columns, CDC metadata, and other system-injected columns that
# are never present on the source side or carry no migration-relevant data.
# ─────────────────────────────────────────────────────────────────────────────

STATIC_EXCLUDE_COLUMNS: list = [
    "_fivetran_synced",
    "_fivetran_deleted",
    "_fivetran_id",
    "_fivetran_index",
    "_fivetran_start",
    "_fivetran_end",
    "_fivetran_active",
]

_EXCLUSIONS_DIR = _SRC_DIR.parent / "config"

# One exclusions file per SOURCE database type. Whatever is listed in a given
# file is excluded from BOTH that source type AND the Snowflake target — e.g.
# everything in postgresql_exclusions.yaml applies to PostgreSQL source columns
# and their Snowflake counterparts, regardless of which PostgreSQL database/table.
_EXCLUSION_FILE_BY_DB_TYPE = {
    "postgresql": _EXCLUSIONS_DIR / "postgresql_exclusions.yaml",
    "mssql":      _EXCLUSIONS_DIR / "mssql_exclusions.yaml",
    "athena":     _EXCLUSIONS_DIR / "athena_exclusions.yaml",
    "redshift":   _EXCLUSIONS_DIR / "redshift_exclusions.yaml",
}


def _exclusions_path_for(db_type: str) -> Path:
    """Resolve the exclusions YAML file for a given SOURCE database type."""
    key = _normalize_db_type(db_type or "postgresql")
    return _EXCLUSION_FILE_BY_DB_TYPE.get(key, _EXCLUSION_FILE_BY_DB_TYPE["postgresql"])


def _load_global_user_exclusions(db_type: str) -> list:
    """
    Load user-defined global column exclusions from the exclusions YAML file
    that corresponds to `db_type` (postgresql/mssql/athena).
    Returns a list of lowercase column names that should be excluded from
    ALL tables of that source type, in BOTH source and Snowflake target.
    """
    import yaml
    path = _exclusions_path_for(db_type)
    try:
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        entries = data.get("user_global_exclusions", [])
        if not isinstance(entries, list):
            return []
        return [
            str(e.get("column_name", "")).strip().lower()
            for e in entries
            if isinstance(e, dict) and e.get("column_name", "").strip()
        ]
    except Exception:
        return []


def _save_global_user_exclusion(db_type: str, column_name: str, reason: str, added_by: str = "") -> bool:
    """
    Append a new global column exclusion to the exclusions YAML file for
    `db_type` (postgresql/mssql/athena). The column will be excluded from
    every table of that source type AND the Snowflake target.
    Returns True on success.
    """
    import yaml
    import datetime
    path = _exclusions_path_for(db_type)
    try:
        data = {}
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

        if "user_global_exclusions" not in data:
            data["user_global_exclusions"] = []

        # Check for duplicates
        existing = [
            str(e.get("column_name", "")).strip().lower()
            for e in data["user_global_exclusions"]
            if isinstance(e, dict)
        ]
        if column_name.lower() in existing:
            return False  # Already exists

        data["user_global_exclusions"].append({
            "column_name":  column_name.lower(),
            "reason":       reason,
            "applies_to":   ["source", "target"],
            "added_by":     added_by or "CLI",
            "date_added":   datetime.date.today().isoformat(),
        })

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return True
    except Exception:
        return False


def _remove_global_user_exclusion(db_type: str, column_name: str) -> bool:
    """
    Remove a previously user-saved global column exclusion from the exclusions
    YAML file for `db_type` (postgresql/mssql/athena). Only removes entries
    under `user_global_exclusions` — the built-in STATIC_EXCLUDE_COLUMNS list
    is code-defined and cannot be removed this way. Returns True if a matching
    entry was found and removed.
    """
    import yaml
    path = _exclusions_path_for(db_type)
    try:
        if not path.exists():
            return False
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        entries = data.get("user_global_exclusions", [])
        if not isinstance(entries, list):
            return False

        target = column_name.strip().lower()
        remaining = [
            e for e in entries
            if not (isinstance(e, dict) and str(e.get("column_name", "")).strip().lower() == target)
        ]
        if len(remaining) == len(entries):
            return False  # Nothing matched

        data["user_global_exclusions"] = remaining
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return True
    except Exception:
        return False


def _get_all_exclusions(db_type: str) -> list:
    """
    Return merged list of all globally excluded column names (lowercase) for
    `db_type` (postgresql/mssql/athena). Combines: STATIC_EXCLUDE_COLUMNS +
    user_global_exclusions from that source type's exclusions YAML file.
    Applied to BOTH that source type and the Snowflake target.
    """
    static = [c.lower() for c in STATIC_EXCLUDE_COLUMNS]
    user   = _load_global_user_exclusions(db_type)
    seen   = set()
    merged = []
    for c in static + user:
        if c not in seen:
            seen.add(c)
            merged.append(c)
    return merged

_PROFILES_PATH = Path.home() / ".migration-validator" / "profiles.json"


class ConnectionProfileManager:
    """
    Manage named connection profiles stored in ~/.migration-validator/profiles.json.

    Profile schema (each key = profile name):
    {
      "fms-dev": {
        "source": {
          "db_type": "postgresql",
          "host": "db-dev.internal",
          "port": 5432,
          "database": "fms",
          "schema": "public",
          "username": "reader",
          "password": "..."      ← stored locally, never committed
        },
        "snowflake": {
          "account": "myorg-myaccount",
          "database": "DEV_EDGE_BRONZE",
          "schema": "STOREDGE_FMS_PUBLIC",
          "username": "analyst@company.com",
          "password": "...",
          "warehouse": "",
          "role": ""
        },
        "created_at": "2026-08-12T14:00:00"
      }
    }
    """

    def __init__(self, path: Path = _PROFILES_PATH):
        self._path = path

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def list_profiles(self) -> list:
        return sorted(self._load().keys())

    def get_profile(self, name: str) -> Optional[dict]:
        return self._load().get(name)

    def save_profile(self, name: str, source: dict, snowflake: dict) -> None:
        import datetime
        data = self._load()
        data[name] = {
            "source":    source,
            "snowflake": snowflake,
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        self._save(data)

    def delete_profile(self, name: str) -> bool:
        data = self._load()
        if name not in data:
            return False
        del data[name]
        self._save(data)
        return True

    def apply_source_env(self, profile: dict) -> None:
        """Push profile's source credentials into the process env (SOURCE_* keys)."""
        src = profile.get("source", {})
        os.environ["SOURCE_TYPE"]     = _normalize_db_type(src.get("db_type", "postgresql"))
        os.environ["SOURCE_HOST"]     = src.get("host", "")
        os.environ["SOURCE_PORT"]     = str(src.get("port", 5432))
        os.environ["SOURCE_DATABASE"] = src.get("database", "")
        os.environ["SOURCE_SCHEMA"]   = src.get("schema", "")
        os.environ["SOURCE_USERNAME"] = src.get("username", "")
        os.environ["SOURCE_PASSWORD"] = src.get("password", "")

    def apply_snowflake_env(self, profile: dict) -> None:
        """Push profile's Snowflake credentials into the process env."""
        sf = profile.get("snowflake", {})
        os.environ["SNOWFLAKE_ACCOUNT"]   = sf.get("account", "")
        os.environ["SNOWFLAKE_DATABASE"]  = sf.get("database", "")
        os.environ["SNOWFLAKE_SCHEMA"]    = sf.get("schema", "")
        os.environ["SNOWFLAKE_USERNAME"]  = sf.get("username", "")
        os.environ["SNOWFLAKE_PASSWORD"]  = sf.get("password", "")
        if sf.get("warehouse"):
            os.environ["SNOWFLAKE_WAREHOUSE"] = sf["warehouse"]
        if sf.get("role"):
            os.environ["SNOWFLAKE_ROLE"] = sf["role"]


_profile_mgr = ConnectionProfileManager()


def _resolve_connection_profile(profile_name: str) -> Optional[dict]:
    """
    Load a named profile, apply its env vars, and return the source registry dict
    compatible with existing helper functions (_override_source_env / _make_source_extractor).
    Returns None if profile not found.
    """
    profile = _profile_mgr.get_profile(profile_name)
    if not profile:
        _err(f"Profile '{profile_name}' not found. Run: python validate_cli.py profiles")
        return None

    src = profile["source"]
    _profile_mgr.apply_source_env(profile)
    _profile_mgr.apply_snowflake_env(profile)

    db_type = _normalize_db_type(src.get("db_type", "postgresql"))
    db_label = _DB_TYPE_LABELS.get(db_type, db_type)
    _ok(f"Using profile '{profile_name}'  —  {db_label}  {src['host']}:{src['port']}/{src['database']}.{src['schema']}")

    return {
        "index":    1,
        "prefix":   "SOURCE_",
        "db_type":  db_type,
        "db_label": db_label,
        "host":     src["host"],
        "port":     str(src.get("port", 5432)),
        "database": src["database"],
        "schema":   src["schema"],
        "username": src["username"],
        "label":    f"profile:{profile_name}",
    }


def _save_session_as_profile(rec: dict, sf_database: str, sf_schema: str) -> None:
    """
    After a successful run, offer to save the used connection as a named profile.
    """
    _blank()
    save = input(
        f"  {_C.DIM}Save this connection as a reusable profile? [y/N]: {_C.RESET}"
    ).strip().lower()
    if save not in ("y", "yes"):
        return

    existing = _profile_mgr.list_profiles()
    if existing:
        _dim(f"  Existing profiles: {', '.join(existing)}")
    name = input(f"  {_C.BOLD}Profile name{_C.RESET} (e.g. fms-dev): ").strip()
    if not name:
        _warn("Empty name — profile not saved.")
        return

    source_dict = {
        "db_type":  rec.get("db_type", "postgresql"),
        "host":     rec.get("host", ""),
        "port":     int(rec.get("port") or 5432),
        "database": rec.get("database", ""),
        "schema":   rec.get("schema", ""),
        "username": rec.get("username", ""),
        "password": os.getenv("SOURCE_PASSWORD", ""),
    }
    snowflake_dict = {
        "account":   os.getenv("SNOWFLAKE_ACCOUNT", ""),
        "database":  sf_database,
        "schema":    sf_schema,
        "username":  os.getenv("SNOWFLAKE_USERNAME", ""),
        "password":  os.getenv("SNOWFLAKE_PASSWORD", ""),
        "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE", ""),
        "role":      os.getenv("SNOWFLAKE_ROLE", ""),
    }
    _profile_mgr.save_profile(name, source_dict, snowflake_dict)
    _ok(f"Profile '{name}' saved to {_PROFILES_PATH}")


# ─────────────────────────────────────────────────────────────────────────────
# MODEL SELECTOR — Interactive + CLI
# ─────────────────────────────────────────────────────────────────────────────

def _get_display_models(verbose_probe: bool = False) -> list:
    """
    Return the list of models to show the user.
    If a DIAL key is configured, probe the API and return only working models.
    Falls back to the full AVAILABLE_MODELS list on any probing error.
    """
    from ai_transformation import AVAILABLE_MODELS
    from model_probe import get_working_models

    dial_key     = os.getenv("DIAL_API_KEY", "")
    api_base     = os.getenv("DIAL_API_BASE",    "https://ai-proxy.lab.epam.com")
    api_version  = os.getenv("DIAL_API_VERSION", "2025-04-01-preview")

    if not dial_key:
        return AVAILABLE_MODELS

    working = get_working_models(
        AVAILABLE_MODELS, dial_key, api_base, api_version, verbose=verbose_probe
    )
    return working if working else AVAILABLE_MODELS


def _select_model_interactive(current_model: str) -> str:
    """
    Show a numbered model list and let the user choose.
    Adapts to active backend:
      - Claude direct: shows CLAUDE_DIRECT_MODELS, saves to CLAUDE_MODEL env var
      - EPAM DIAL:     shows AVAILABLE_MODELS (probed), saves to DIAL_MODEL env var
    Returns the selected model name (unchanged if user presses Enter).
    """
    from ai_transformation.ai_rule_mapper import MODEL_DESCRIPTIONS, CLAUDE_DIRECT_MODELS

    _head("🤖  AI MODEL SELECTION")
    print()

    dial_key   = os.getenv("DIAL_API_KEY", "")
    claude_key = os.getenv("CLAUDE_API_KEY", "")

    # ── Claude direct backend ────────────────────────────────────────────
    if claude_key and not dial_key:
        current_model = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
        print(f"  Backend       : {_C.CYAN}Claude Direct{_C.RESET}  (DIAL_API_KEY not set)")
        print(f"  Current model : {_C.GREEN}{current_model}{_C.RESET}")
        print()
        print(f"  {'#':<4} {'Model ID':<42} Description")
        _sep("─", 78)

        for idx, model in enumerate(CLAUDE_DIRECT_MODELS, 1):
            info   = MODEL_DESCRIPTIONS.get(model, ("Anthropic", model, "Claude direct API"))
            marker = f"  {_C.GREEN}← current{_C.RESET}" if model == current_model else ""
            print(
                f"    {_C.CYAN}[{idx}]{_C.RESET}  "
                f"{model:<42} {_C.DIM}{info[2]}{_C.RESET}{marker}"
            )

        print(f"\n    {_C.DIM}[Enter]  Keep current model ({current_model}){_C.RESET}")
        print()

        choice = input("  Select model number (or press Enter to keep current): ").strip()
        if not choice:
            return current_model
        try:
            num = int(choice) - 1
            if 0 <= num < len(CLAUDE_DIRECT_MODELS):
                selected = CLAUDE_DIRECT_MODELS[num]
                # Persist to CLAUDE_MODEL env var for this session
                os.environ["CLAUDE_MODEL"] = selected
                _ok(f"Claude model set to: {selected}")
                _dim("To persist: update CLAUDE_MODEL in .env")
                return selected
            else:
                _warn(f"Invalid choice '{choice}'. Keeping current model.")
                return current_model
        except ValueError:
            _warn(f"Invalid input '{choice}'. Keeping current model.")
            return current_model

    # ── EPAM DIAL backend ───────────────────────────────────────────────
    if not dial_key:
        _warn("No API key configured — AI mode is inactive.")
        _dim("Set DIAL_API_KEY (EPAM DIAL) or CLAUDE_API_KEY (Anthropic direct) in .env")
        _dim("Or run: python validate_cli.py  →  choose [8] Configure API key")
        return current_model

    _dim("Checking which DIAL models are available (cached 24h)...")
    display_models = _get_display_models()
    _ok(f"{len(display_models)} working DIAL model(s) found")
    print()

    print(f"  Current model : {_C.GREEN}{current_model}{_C.RESET}\n")
    print(f"  {'#':<4} {'Provider':<12} {'Model ID':<36} Description")
    _sep("─", 80)

    # Group by provider
    by_provider: dict = {}
    providers_seen = []
    for model in display_models:
        info = MODEL_DESCRIPTIONS.get(model, ("Other", model, "Available via EPAM DIAL"))
        provider = info[0]
        if provider not in by_provider:
            by_provider[provider] = []
            providers_seen.append(provider)
        by_provider[provider].append((model, info[2]))

    numbered: list = []
    idx = 1
    for provider in providers_seen:
        for model, desc in by_provider[provider]:
            marker = f"  {_C.GREEN}← current{_C.RESET}" if model == current_model else ""
            print(
                f"    {_C.CYAN}[{idx}]{_C.RESET}  "
                f"{_C.YELLOW}{provider:<12}{_C.RESET}"
                f"{model:<36} {_C.DIM}{desc}{_C.RESET}{marker}"
            )
            numbered.append(model)
            idx += 1

    print(f"\n    {_C.DIM}[Enter]  Keep current model  ({current_model}){_C.RESET}")
    print()

    choice = input("  Select model number (or press Enter to keep current): ").strip()
    if not choice:
        return current_model

    try:
        num = int(choice) - 1
        if 0 <= num < len(numbered):
            selected = numbered[num]
            os.environ["DIAL_MODEL"] = selected
            _ok(f"DIAL model selected: {selected}")
            _dim("To persist: update DIAL_MODEL in .env")
            return selected
        else:
            _warn(f"Invalid choice '{choice}'. Keeping current model.")
            return current_model
    except ValueError:
        _warn(f"Invalid input '{choice}'. Keeping current model.")
        return current_model


def _list_models_cmd():
    """Print available models — adapts to active backend (DIAL or Claude direct)."""
    from ai_transformation import AVAILABLE_MODELS
    from ai_transformation.ai_rule_mapper import MODEL_DESCRIPTIONS, CLAUDE_DIRECT_MODELS

    _banner()

    dial_key   = os.getenv("DIAL_API_KEY", "")
    claude_key = os.getenv("CLAUDE_API_KEY", "")

    # ── CLAUDE DIRECT BACKEND ─────────────────────────────────────────────
    if claude_key and not dial_key:
        current = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
        _head("🤖  AVAILABLE AI MODELS  (Anthropic Claude Direct)")
        print()
        print(f"  Backend       : {_C.CYAN}Claude Direct{_C.RESET}  (DIAL_API_KEY not set)")
        print(f"  CLAUDE_API_KEY: {_C.GREEN}✓ configured{_C.RESET}")
        print(f"  Current model : {_C.GREEN}{current}{_C.RESET}")
        print()
        print(f"  {_C.BOLD}To switch models: update CLAUDE_MODEL in .env  or run [8] Configure API key{_C.RESET}")
        print()
        print(f"  {'#':<4} {'Provider':<12} {'Model ID':<40} Description")
        _sep("─", 90)

        for idx, model in enumerate(CLAUDE_DIRECT_MODELS, 1):
            info   = MODEL_DESCRIPTIONS.get(model, ("Anthropic", model, "Claude direct API"))
            marker = f"  {_C.GREEN}← active{_C.RESET}" if model == current else ""
            print(
                f"  {_C.DIM}{idx:<4}{_C.RESET}"
                f"{_C.YELLOW}{'Anthropic':<12}{_C.RESET}"
                f"{_C.CYAN}{model:<40}{_C.RESET}"
                f"{_C.DIM}{info[2]}{_C.RESET}{marker}"
            )
        print()
        _dim(f"Showing {len(CLAUDE_DIRECT_MODELS)} Claude direct model(s).")
        _dim("To change: update CLAUDE_MODEL=<model_name> in .env")
        _dim("To switch to DIAL: set DIAL_API_KEY in .env (takes priority over Claude)")
        print()
        return

    # ── EPAM DIAL BACKEND (default) ───────────────────────────────────────
    _head("🤖  AVAILABLE AI MODELS  (via EPAM DIAL)")
    print()

    current = os.getenv("DIAL_MODEL", "gpt-4o")
    status  = f"{_C.GREEN}✓ ACTIVE{_C.RESET}" if dial_key else f"{_C.YELLOW}✗ NOT CONFIGURED{_C.RESET}"
    print(f"  Backend       : {_C.GREEN}EPAM DIAL{_C.RESET}")
    print(f"  DIAL API Key  : {status}")
    print(f"  Current model : {_C.GREEN}{current}{_C.RESET}")
    print()

    if dial_key:
        _dim("Probing API to find working models (cached 24h)...")
        display_models = _get_display_models(verbose_probe=False)
        skipped = len(AVAILABLE_MODELS) - len(display_models)
        _ok(f"{len(display_models)} working model(s) on your key"
            + (f"  ({skipped} unavailable — hidden)" if skipped else ""))
    else:
        display_models = AVAILABLE_MODELS
        _warn("No DIAL API key — showing all models (availability not verified)")
        _dim("  To use Claude instead: set CLAUDE_API_KEY in .env")
    print()

    # Group by provider
    providers_seen = []
    by_provider: dict = {}
    for model in display_models:
        info = MODEL_DESCRIPTIONS.get(model, ("Other", model, "Available via EPAM DIAL"))
        provider = info[0]
        if provider not in by_provider:
            by_provider[provider] = []
            providers_seen.append(provider)
        by_provider[provider].append((model, info[1], info[2]))

    print(f"  {'#':<4} {'Provider':<12} {'Model ID':<38} {'Display Name':<26} Description")
    _sep("─", 98)

    idx = 1
    for provider in providers_seen:
        for model, display, desc in by_provider[provider]:
            marker = f" {_C.GREEN}← active{_C.RESET}" if model == current else ""
            print(
                f"  {_C.DIM}{idx:<4}{_C.RESET}"
                f"{_C.YELLOW}{provider:<12}{_C.RESET}"
                f"{_C.CYAN}{model:<38}{_C.RESET}"
                f"{display:<26} {_C.DIM}{desc}{_C.RESET}{marker}"
            )
            idx += 1
    print()
    _dim(f"Showing {len(display_models)} model(s) available via EPAM DIAL.")
    _dim("To set default: add DIAL_MODEL=<model_name> to your .env file")
    _dim("To select per-run: python validate_cli.py generate --model gpt-4o-mini")
    _dim("To refresh availability: delete .dial_model_cache.json next to .env")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND: setup  (first-run wizard)
# ─────────────────────────────────────────────────────────────────────────────

def cmd_setup(args):
    """
    Launch the first-run setup wizard to configure databases and AI in .env.

    The wizard:
      1. Asks how many source database servers you have (1–5)
      2. Guides you through credentials for each source server
      3. Configures the Snowflake target
      4. Optionally configures DIAL / AI API key
      5. Tests all connections
      6. Writes a clean .env file
    """
    from setup_wizard import run_wizard
    run_wizard()


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND: generate  (single table)
# ─────────────────────────────────────────────────────────────────────────────

def cmd_generate(args):
    """
    Validation workflow:
      Single table  : pick source → pick table → generate SQL + YAML
      Multi-table   : --source N --tables t1,t2,t3 → run pipeline per table
      Fast-path     : --source-table X --sf-table Y → non-interactive

    New flags:
      --source N          Pre-select SRC_N connection (skip interactive picker)
      --tables t1,t2,...  Run once per table (requires --source N)
      --exclude c1,c2,... Exclude columns from validation
    """
    from validation_pipeline import ValidationPipeline
    from rule_book import rule_book

    _banner()

    stats = rule_book.stats()
    _dim(f"Rule book: {stats['base_rules']} base + {stats['learned_rules']} learned = {stats['total_rules']} rules")

    # Pick the correct model env var based on active backend
    _dial_key   = os.getenv("DIAL_API_KEY", "")
    _claude_key = os.getenv("CLAUDE_API_KEY", "")
    if getattr(args, "model", None):
        current_model = args.model
    elif _dial_key:
        current_model = os.getenv("DIAL_MODEL", "gpt-4o")
    elif _claude_key:
        current_model = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
    else:
        current_model = os.getenv("DIAL_MODEL", "gpt-4o")
    rec = None  # set when user picks a source connection interactively

    # Parse --exclude  →  merge user list with ALL global exclusions (static + saved).
    # The source database type isn't known yet (connection not picked), so this
    # uses the PostgreSQL exclusions file as a placeholder default; it is
    # recomputed below with the correct per-source-type file once the source
    # connection (and its db_type) has been resolved.
    exclude_raw  = getattr(args, "exclude", None) or ""
    user_exclude = [c.strip().lower() for c in exclude_raw.split(",") if c.strip()] if exclude_raw else []
    all_global   = _get_all_exclusions("postgresql")
    seen = set()
    exclude_cols = []
    for c in all_global + user_exclude:
        if c not in seen:
            seen.add(c)
            exclude_cols.append(c)

    # ── Connection profile fast-path: --connection-profile <name> ────────────
    profile_name = getattr(args, "connection_profile", None)
    if profile_name:
        _warn("Connection profiles are disabled. Use credentials from .env.")
        profile_name = None

    # ── Multi-table parameterized flow: --source N --tables t1,t2,... ────────
    tables_raw = getattr(args, "tables", None) or ""
    if tables_raw:
        return _run_parameterized_tables(args, current_model, exclude_cols)

    _head("📋  SINGLE-TABLE VALIDATION  (SQL + YAML)")

    # ── CLI args fast-path (non-interactive) ─────────────────────────────────
    pg_table    = getattr(args, "source_table",    None)
    pg_schema   = getattr(args, "source_schema",   None)
    pg_database = getattr(args, "source_database", None)
    sf_table    = getattr(args, "sf_table",    None)
    sf_schema   = getattr(args, "sf_schema",   None)
    sf_database = getattr(args, "sf_database", None)
    src_label   = ""

    # Connection profile already resolved above — sync env/label from rec
    if rec and profile_name:
        pg_schema   = pg_schema   or rec["schema"]
        pg_database = pg_database or rec["database"]
        src_label   = f"{pg_database}.{pg_schema}"
        _override_source_env(rec)

    # Pre-select connection via --source N
    source_index = getattr(args, "source_index", None)
    if source_index is not None and not (pg_table and sf_table) and rec is None:
        rec = _get_connection_by_index(source_index)
        if rec:
            pg_schema   = pg_schema   or rec["schema"]
            pg_database = pg_database or rec["database"]
            src_label   = f"{pg_database}.{pg_schema}"
            _override_source_env(rec)

    if pg_table and sf_table:
        # All args supplied — skip interactive pickers
        pg_schema   = pg_schema   or os.getenv("SOURCE_SCHEMA",    "public")
        pg_database = pg_database or os.getenv("SOURCE_DATABASE",  "")
        sf_schema   = sf_schema   or os.getenv("SNOWFLAKE_SCHEMA", "")
        sf_database = sf_database or os.getenv("SNOWFLAKE_DATABASE", "")
        src_label   = src_label or f"{pg_database}.{pg_schema}"
    else:
        # ── Interactive: pick connection ─────────────────────────────────────
        if rec is None and source_index is None:
            rec = _pick_source_connection()
            if rec is None:
                return
            pg_database = pg_database or rec["database"]
            _override_source_env(rec)
        elif rec is None:
            # --source was given but no --source-table: need to pick table interactively
            rec = _get_connection_by_index(source_index)
            if rec is None:
                return
            pg_database = pg_database or rec["database"]
        else:
            pg_database = pg_database or rec["database"]

        # ── Interactive: pick database then schema ────────────────────────────
        if not pg_database or pg_database == rec.get("database", ""):
            pg_database = _pick_database_from_source(rec)  # updates rec["database"]
        if not pg_schema:
            pg_schema = _pick_schema_from_source(rec)
            if not pg_schema:
                _err("No schema selected.")
                return
        rec["schema"] = pg_schema  # keep rec in sync for _pick_table_from_source
        src_label = f"{pg_database}.{pg_schema}"

        # ── Interactive: pick source table ───────────────────────────────────
        if not pg_table:
            pg_table = _pick_table_from_source(rec)
            if not pg_table:
                _err("No table selected.")
                return

        # ── Interactive: pick Snowflake target table ─────────────────────────
        if not sf_table:
            sf_table, sf_schema, sf_database = _pick_snowflake_table(pg_table)
            if not sf_table:
                _err("No Snowflake table selected.")
                return
        else:
            sf_schema   = sf_schema   or os.getenv("SNOWFLAKE_SCHEMA",   "")
            sf_database = sf_database or os.getenv("SNOWFLAKE_DATABASE", "")

    # Recompute global exclusions now that the source connection (and its
    # db_type) is known, so the correct <db_type>_exclusions.yaml is used —
    # its entries apply to BOTH this source and the Snowflake target.
    _src_db_type = _normalize_db_type(rec["db_type"]) if rec else _normalize_db_type(os.getenv("SOURCE_TYPE", "postgresql"))
    _all_global  = _get_all_exclusions(_src_db_type)
    _seen = set()
    exclude_cols = []
    for c in _all_global + user_exclude:
        if c not in _seen:
            _seen.add(c)
            exclude_cols.append(c)

    # ── Config summary — detect active backend correctly ────────────────────
    _dial_key_chk   = os.getenv("DIAL_API_KEY", "")
    _claude_key_chk = os.getenv("CLAUDE_API_KEY", "")
    if _dial_key_chk:
        ai_status  = f"{_C.GREEN}✓ EPAM DIAL active{_C.RESET}"
        ai_backend = "DIAL"
    elif _claude_key_chk:
        ai_status  = f"{_C.CYAN}✓ Claude Direct active{_C.RESET}"
        ai_backend = "Claude"
    else:
        ai_status  = f"{_C.YELLOW}⚠ No API key set — set DIAL_API_KEY or CLAUDE_API_KEY{_C.RESET}"
        ai_backend = "None"
    _ = ai_backend  # used in summary print below
    if exclude_cols:
        _static_set = {c.lower() for c in STATIC_EXCLUDE_COLUMNS}
        user_extra  = [c for c in exclude_cols if c.lower() not in _static_set]
        excl_str = (
            f"\n    Static excl : {_C.DIM}{', '.join(STATIC_EXCLUDE_COLUMNS[:5])} …({len(STATIC_EXCLUDE_COLUMNS)} total){_C.RESET}"
            + (f"\n    Also excl   : {_C.YELLOW}{', '.join(user_extra)}{_C.RESET}" if user_extra else "")
        )
    else:
        excl_str = ""
    print(f"""
  {_C.BOLD}Validation Plan:{_C.RESET}
    Source      : {_C.GREEN}{src_label}.{pg_table}{_C.RESET}
    Target      : {_C.CYAN}Snowflake  {sf_database}.{sf_schema}.{sf_table}{_C.RESET}
    AI Mode     : {ai_status}
    Model       : {_C.CYAN}{current_model}{_C.RESET}{excl_str}
""")


    # Source/target are picked above; everything else (rule book review, AI
    # model change, layer choice) uses defaults so generation runs straight
    # through — pass --model / --layer on the command line to override.
    _layer = getattr(args, "layer", None) or "bronze"
    _output_dir = Path(__file__).parent.parent / "Project" / "config" / _layer

    # ── Run pipeline ──────────────────────────────────────────────────────────
    print()
    try:
        _src_ext = _make_source_extractor(rec) if rec is not None else None
        pipeline = ValidationPipeline(model=current_model, source_extractor=_src_ext)
        result   = pipeline.run(
            pg_schema=pg_schema,
            pg_table=pg_table,
            sf_schema=sf_schema,
            sf_table=sf_table,
            sf_database=sf_database,
            pg_database=pg_database,
            exclude_columns=exclude_cols or None,
            output_dir=_output_dir,
        )
        _show_output_summary(result, pg_database=pg_database, source_db_type=_src_db_type)

    except Exception as exc:
        _err(f"Generation failed: {exc}")
        print()
        print("  Troubleshooting:")
        print("    • Check SOURCE_* vars in .env for your source DB")
        print("    • Check SNOWFLAKE_* vars in .env for Snowflake")
        print("    • Run: python validate_cli.py connections  for diagnostics")
        sys.exit(1)


def _show_output_summary(result, pg_database: str = "", source_db_type: str = "postgresql") -> None:
    """Pretty-print where the output files were saved, then offer to execute queries."""
    source_label = _DB_TYPE_LABELS.get(_normalize_db_type(source_db_type), source_db_type or "Source")
    _sep("═")
    print(f"\n  {_C.BOLD}{_C.GREEN}✓  GENERATION COMPLETE{_C.RESET}")
    _sep()
    print(f"\n  {_C.BOLD}Table          :{_C.RESET} {result.table_name}")
    print(f"  {_C.BOLD}Generated by   :{_C.RESET} {result.generated_by.upper()}")
    print(f"  {_C.BOLD}AI Model       :{_C.RESET} {result.model_used}")
    print(f"  {_C.BOLD}Active columns :{_C.RESET} {result.active_columns}")
    if result.skipped_columns:
        print(f"  {_C.BOLD}Skipped        :{_C.RESET} {', '.join(result.skipped_columns)}")
    fivetran_str = (
        f"{_C.GREEN}YES — WHERE _FIVETRAN_ACTIVE = TRUE{_C.RESET}"
        if result.has_fivetran_active
        else f"{_C.DIM}NO{_C.RESET}"
    )
    print(f"  {_C.BOLD}Fivetran filter:{_C.RESET} {fivetran_str}")

    print(f"\n  {_C.BOLD}Output files:{_C.RESET}")
    if getattr(result, "count_yaml_path", None):
        print(f"    {_C.CYAN}📋 Count YAML   :{_C.RESET}  {result.count_yaml_path}")
    print(f"    {_C.CYAN}📋 Full YAML    :{_C.RESET}  {result.yaml_path}")
    if getattr(result, "dynamic_suite_yaml_path", None):
        print(f"    {_C.CYAN}📋 Dynamic YAML :{_C.RESET}  {result.dynamic_suite_yaml_path}")



# ─────────────────────────────────────────────────────────────────────────────
# Rule book review (shown before generation)
# ─────────────────────────────────────────────────────────────────────────────

def _rule_review_step(args, current_model: str) -> str:
    """
    Show rule book summary and allow user to view / add rules before
    the AI generates queries. Returns (possibly updated) model string.
    """
    from rule_book import rule_book

    stats   = rule_book.stats()
    learned = rule_book.learned_rules()

    print(f"\n  {_C.BOLD}{'─' * 60}{_C.RESET}")
    print(f"  {_C.BOLD}📚  RULE BOOK REVIEW  (Before Query Generation){_C.RESET}")
    print(f"  {_C.BOLD}{'─' * 60}{_C.RESET}")
    _dim("The AI uses these rules to decide how to normalise each column.")
    _dim("Review now and add any missing rules before proceeding.")
    print()
    print(f"    Base rules   : {_C.GREEN}{stats['base_rules']}{_C.RESET}")
    print(f"    Learned rules: {_C.CYAN}{stats['learned_rules']}{_C.RESET}")
    print(f"    Total        : {_C.BOLD}{stats['total_rules']}{_C.RESET}")
    print()

    if learned:
        print(f"  {_C.BOLD}Your learned rules (injected into AI prompt):{_C.RESET}")
        for r in learned:
            print(f"    {_C.CYAN}• {r.id}{_C.RESET}  —  {r.description[:65]}")
            print(f"      PG: {r.pg_sql_template}")
            print(f"      SF: {r.sf_sql_template}")
        print()

    print(f"  Options:")
    print(f"    {_C.GREEN}[v]{_C.RESET}  View all rules in detail")
    print(f"    {_C.CYAN}[a]{_C.RESET}  Add a new rule to the rule book")
    print(f"    {_C.DIM}[c]{_C.RESET}  Continue — rules look good")
    print()

    while True:
        choice = input("  Choice [v/a/C]: ").strip().lower()
        if choice in ("", "c", "continue"):
            _ok("Rules confirmed. Proceeding...")
            break
        elif choice == "v":
            _print_rules_full(rule_book)
            print(f"\n  {_C.CYAN}[a]{_C.RESET} Add rule  |  {_C.DIM}[c]{_C.RESET} Continue")
        elif choice == "a":
            cmd_add_rule(args)
            updated = rule_book.stats()
            _ok(f"Rule book updated: {updated['total_rules']} total rules")
        else:
            _warn(f"Unknown choice '{choice}'. Enter v, a, or c.")

    return current_model


def _print_rules_full(rb) -> None:
    """Print detailed view of all rules."""
    print(f"\n  {_C.BOLD}── BASE RULES ─────────────────────────────────────────────{_C.RESET}")
    for r in rb.base_rules():
        print(f"\n    {_C.GREEN}{_C.BOLD}{r.id}{_C.RESET}  {_C.DIM}({r.display_name}){_C.RESET}")
        print(f"      What  : {r.description[:90]}")
        print(f"      PG SQL: {r.pg_sql_template}")
        print(f"      SF SQL: {r.sf_sql_template}")

    learned = rb.learned_rules()
    if learned:
        print(f"\n  {_C.BOLD}── YOUR LEARNED RULES ─────────────────────────────────────{_C.RESET}")
        for r in learned:
            added = r.learned_at[:10] if r.learned_at else "unknown"
            print(f"\n    {_C.CYAN}{_C.BOLD}{r.id}{_C.RESET}  {_C.DIM}added {added}{_C.RESET}")
            print(f"      What  : {r.description}")
            print(f"      When  : {r.when_to_apply}")
            print(f"      PG SQL: {r.pg_sql_template}")
            print(f"      SF SQL: {r.sf_sql_template}")
            if r.example:
                print(f"      Example: {r.example}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND: rules
# ─────────────────────────────────────────────────────────────────────────────

def cmd_rules(args):
    """Display the full rule book with all transformation rules."""
    from rule_book import rule_book

    _banner()
    _head("📚  RULE BOOK — All Transformation Rules")

    stats = rule_book.stats()
    print(f"\n  Base rules   : {_C.GREEN}{stats['base_rules']}{_C.RESET}")
    print(f"  Learned rules: {_C.CYAN}{stats['learned_rules']}{_C.RESET}")
    print(f"  Total        : {_C.BOLD}{stats['total_rules']}{_C.RESET}")
    print()

    # Base rules
    print(f"{_C.BOLD}  ── BASE RULES (Built-in, apply automatically) ──────────────{_C.RESET}")
    for r in rule_book.base_rules():
        print(f"\n  {_C.BOLD}{_C.GREEN}{r.id}{_C.RESET}  {_C.DIM}({r.display_name}){_C.RESET}")
        print(f"    Description : {r.description[:100]}")
        print(f"    Triggers on : {r.source_type} → {r.target_type}")
        print(f"    PG SQL      : {r.pg_sql_template}")
        print(f"    SF SQL      : {r.sf_sql_template}")

    # Learned rules
    learned = rule_book.learned_rules()
    if learned:
        print(f"\n{_C.BOLD}  ── YOUR LEARNED RULES (injected into AI prompt) ───────────{_C.RESET}")
        for r in learned:
            added = r.learned_at[:10] if r.learned_at else "unknown"
            print(f"\n  {_C.BOLD}{_C.CYAN}{r.id}{_C.RESET}  {_C.DIM}(added {added}){_C.RESET}")
            print(f"    Description : {r.description}")
            print(f"    Apply when  : {r.when_to_apply}")
            print(f"    PG SQL      : {r.pg_sql_template}")
            print(f"    SF SQL      : {r.sf_sql_template}")
            if r.example:
                print(f"    Example     : {r.example}")
    else:
        print(f"\n  {_C.DIM}No learned rules yet. Use 'add-rule' to add your first custom rule.{_C.RESET}")

    # Also print the AI prompt block structure
    print(f"\n{_C.BOLD}  ── RULE APPLICATION ORDER ──────────────────────────────────{_C.RESET}")
    print(f"  {_C.DIM}(Innermost → Outermost — applied per column){_C.RESET}")
    order = [
        "integer / uuid / json / bytea  (type-specific inner transform)",
        "boolean  (CASE WHEN → '1'/'0')",
        "timestamp_tz  (UTC conversion → format)",
        "timestamp_ntz / date  (format only)",
        "numeric  (ROUND to 2dp)",
        "text  (TRIM)",
        "NULL placeholder  ← ALWAYS LAST: COALESCE(…, '<<NULL>>')",
    ]
    for i, step in enumerate(order, 1):
        print(f"    {i}. {step}")

    print(f"\n  {_C.BOLD}Fivetran filter (Snowflake side only):{_C.RESET}")
    print(f"    WHERE _FIVETRAN_ACTIVE = TRUE")
    print(f"    {_C.DIM}Auto-applied when _FIVETRAN_ACTIVE column is detected{_C.RESET}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND: add-rule
# ─────────────────────────────────────────────────────────────────────────────

def cmd_add_rule(args):
    """
    Interactive wizard to add a new rule to rule_book_learned.json.
    The rule is saved permanently and injected into ALL future AI prompts.
    """
    from rule_book import rule_book, RuleEntry, RuleValidationError

    _banner()
    _head("➕  ADD NEW RULE TO RULE BOOK")

    print(f"""
  {_C.DIM}Rules you add here are saved to rule_book_learned.json and
  automatically injected into the AI prompt on every future run.
  Use this for column-level transformations not in the base rules
  (e.g. phone number normalisation, currency stripping, etc.).{_C.RESET}
""")

    # ── Collect rule details ──────────────────────────────────────────────────
    rule_id = _prompt("Rule ID (snake_case, e.g. phone_strip)", "").strip()
    if not rule_id:
        _warn("Rule ID cannot be empty. Cancelled.")
        return

    # Normalise to snake_case
    rule_id = rule_id.lower().replace(" ", "_").replace("-", "_")

    # Check for duplicates
    if rule_book.rule_exists(rule_id):
        ow = input(f"  Rule '{rule_id}' already exists. Overwrite? [y/N]: ").strip().lower()
        if ow not in ("y", "yes"):
            _dim("Cancelled.")
            return

    display_name  = _prompt("Display name (e.g. 'Phone Number Strip')", rule_id)
    description   = _prompt("What does this rule do? (plain English)", "")
    when_to_apply = _prompt(
        "When to apply? (e.g. 'VARCHAR phone numbers migrated from PG to Snowflake')", ""
    )
    source_type   = _prompt("Source (PostgreSQL) type that triggers this (e.g. VARCHAR)", "*")
    target_type   = _prompt("Target (Snowflake)  type that triggers this (e.g. VARCHAR)", "*")
    pg_template   = _prompt(
        "PostgreSQL SQL template — use {col} for column reference\n"
        "  e.g. REGEXP_REPLACE({col}, '[^0-9]', '', 'g')",
        "{col}",
    )
    sf_template   = _prompt(
        "Snowflake SQL template — use {col} for column reference\n"
        "  e.g. REGEXP_REPLACE({col}, '[^0-9]', '')",
        "{col}",
    )
    example = _prompt("Optional: example or scenario (press Enter to skip)", "")

    # ── Preview ───────────────────────────────────────────────────────────────
    print(f"""
  {_C.BOLD}Preview:{_C.RESET}
    ID          : {_C.GREEN}{rule_id}{_C.RESET}
    Display     : {display_name}
    Description : {description}
    When        : {when_to_apply}
    Src type    : {source_type}  →  Tgt type : {target_type}
    PG SQL      : {pg_template}
    SF SQL      : {sf_template}
    Example     : {example or '(none)'}
""")

    confirm = input("  Save this rule? [Y/n]: ").strip().lower()
    if confirm in ("n", "no"):
        _dim("Cancelled.")
        return

    entry = RuleEntry(
        id=rule_id,
        display_name=display_name,
        description=description,
        when_to_apply=when_to_apply,
        pg_sql_template=pg_template,
        sf_sql_template=sf_template,
        source_type=source_type,
        target_type=target_type,
        is_learned=True,
        example=example or None,
    )

    try:
        saved = rule_book.save_learned_rule(entry)
    except RuleValidationError as exc:
        _err(f"Rejected: {exc}")
        return

    if saved:
        _ok(f"Rule '{rule_id}' saved to rule_book_learned.json ✓")
        _ok("It will be included in ALL future AI prompts automatically.")
        print(f"\n  {_C.DIM}File: {_SRC_DIR / 'rule_book_learned.json'}{_C.RESET}")
    else:
        _err("Failed to save rule — check write permissions.")


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND: connections  — show the live connection registry
# ─────────────────────────────────────────────────────────────────────────────

def cmd_connections(args):
    """
    Show all configured source connections from .env (SRC_N_* and SOURCE_*)
    with live ping to each one, table count, and Snowflake target summary.
    """
    from setup_wizard import print_connection_registry, _test_connection, SourceConnection, DB_TYPES

    _banner()
    _head("🔌  CONNECTION REGISTRY  (PostgreSQL / MS SQL Server / Athena / Snowflake)")

    env_path = _SRC_DIR.parent / ".env"
    if not env_path.exists():
        _warn("No .env file found. Run:  python validate_cli.py setup")
        return

    registry = [_apply_database_registry(record) for record in print_connection_registry(env_path)]
    if not registry:
        _warn("No source connections found in .env. Run:  python validate_cli.py setup")
        return

    print(f"\n  {_C.BOLD}Source Connections{_C.RESET}  ({len(registry)} configured)\n")
    print(
        f"  {'Slot':<8}{'Type':<20}{'Host':<24}{'Port':<6}"
        f"{'Database':<20}{'Schema':<14}{'Status'}"
    )
    _sep("─")

    has_athena = False
    for rec in registry:
        db_type  = _normalize_db_type(rec["db_type"])
        db_label = _DB_TYPE_LABELS.get(db_type, db_type)
        if db_type == "athena":
            has_athena = True

        if db_type == "athena":
            _dim(f"Pinging Athena region={rec['host'] or 'default'}/{rec['database']} ...")
        else:
            _dim(f"Pinging {rec['host']}:{rec['port']}/{rec['database']}.{rec['schema']} ...")
        try:
            conn = SourceConnection(
                index=rec["index"],
                db_type=db_type,
                host=rec["host"],
                port=int(rec.get("port") or DB_TYPES.get(db_type, ("", 5432))[1]),
                database=rec["database"],
                schema=rec["schema"],
                username=rec["username"],
                password=os.getenv(f"{rec['prefix']}PASSWORD", ""),
                auth=rec.get("auth", ""),
            )
            if db_type == "athena":
                from setup_wizard import _test_athena
                ok, msg, count = _test_athena(
                    conn,
                    s3_output=rec.get("s3_output", ""),
                    region=rec.get("host", ""),
                )
            else:
                ok, msg, count = _test_connection(conn)
            if ok:
                status = f"{_C.GREEN}✓ OK{_C.RESET}  {count} tables"
            else:
                status = f"{_C.RED}✗ FAIL{_C.RESET}  {msg}"
        except Exception as exc:
            status = f"{_C.RED}✗ error: {exc}{_C.RESET}"

        print(
            f"  {_C.CYAN}SRC_{rec['index']:<4}{_C.RESET}"
            f"{db_label:<20}"
            f"{rec['host']:<24}"
            f"{str(rec.get('port','')):<6}"
            f"{_C.GREEN}{rec['database']:<20}{_C.RESET}"
            f"{rec['schema']:<14}"
            f"{status}"
        )

    # Athena placeholder — shown when no Athena source is configured yet
    if not has_athena:
        print(
            f"  {'——':<8}"
            f"{'AWS Athena':<20}"
            f"{'(not configured)':<24}"
            f"{'':<6}{'':<20}{'':<14}"
            f"{_C.DIM}─ add credentials via: python validate_cli.py setup{_C.RESET}"
        )

    # Snowflake target
    sf_account = os.getenv("SNOWFLAKE_ACCOUNT", "")
    sf_db      = os.getenv("SNOWFLAKE_DATABASE", "")
    sf_schema  = os.getenv("SNOWFLAKE_SCHEMA", "")
    sf_user    = os.getenv("SNOWFLAKE_USERNAME", "")
    sf_pass    = os.getenv("SNOWFLAKE_PASSWORD", "")

    print(f"\n  {_C.BOLD}Snowflake Target{_C.RESET}")
    _sep("─")
    if sf_account:
        _dim(f"Pinging Snowflake {sf_account}/{sf_db}.{sf_schema} ...")
        from setup_wizard import _test_snowflake_target
        ok, msg, count = _test_snowflake_target(sf_account, sf_db, sf_schema, sf_user, sf_pass)
        status = f"{_C.GREEN}✓ OK{_C.RESET}  {count} tables — {msg}" if ok else f"{_C.RED}✗ {msg[:60]}{_C.RESET}"
        print(f"  {'TARGET':<8}{'Snowflake':<20}{sf_account:<30}{sf_db:<20}{sf_schema:<14}{status}")
    else:
        _warn("Snowflake not configured. Run:  python validate_cli.py setup")

    print()
    _dim("To reconfigure: python validate_cli.py setup")
    _dim("To list tables: python validate_cli.py list-tables")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND: list-tables  — live table list for ALL configured sources
# ─────────────────────────────────────────────────────────────────────────────

def cmd_list_tables(args):
    """
    List tables in ALL configured source databases (SRC_N_*) and
    the Snowflake target.  Filters by name pattern if --filter is given.
    """
    from setup_wizard import print_connection_registry, SourceConnection, DB_TYPES
    from sql_extractor import ExtractorFactory

    _banner()
    _head("🔍  AVAILABLE TABLES  (all configured connections)")

    env_path = _SRC_DIR.parent / ".env"
    registry = [_apply_database_registry(record) for record in print_connection_registry(env_path)]

    name_filter = getattr(args, "filter", None) or ""

    if not registry:
        _warn("No source connections configured. Run:  python validate_cli.py setup")
        return

    for rec in registry:
        db_label = rec["db_label"]
        _db_type = _normalize_db_type(rec["db_type"])
        if _db_type == "athena":
            print(f"\n  {_C.BOLD}{_C.CYAN}SRC_{rec['index']}  {db_label}{_C.RESET}"
                  f"  —  region={rec['host']}/{rec['database']}")
        else:
            print(f"\n  {_C.BOLD}{_C.CYAN}SRC_{rec['index']}  {db_label}{_C.RESET}"
                  f"  —  {rec['host']}:{rec['port']}/{rec['database']}.{rec['schema']}")
        try:
            extractor = ExtractorFactory.create(
                _db_type,
                host=rec["host"],
                port=int(rec.get("port") or DB_TYPES.get(_db_type, ("", 5432))[1]),
                database=rec["database"],
                username=rec["username"],
                password=os.getenv(f"{rec['prefix']}PASSWORD", ""),
                auth=rec.get("auth", ""),
                s3_output=rec.get("s3_output", ""),
            )
            tables = extractor.list_tables(rec["schema"])
            if name_filter:
                tables = [t for t in tables if name_filter.lower() in t.lower()]
            if tables:
                cols = 3
                for i in range(0, len(tables), cols):
                    row = tables[i:i+cols]
                    print("    " + "  ".join(f"{_C.GREEN}•{_C.RESET} {t:<30}" for t in row))
                _dim(f"  {len(tables)} table(s) in {rec['database']}.{rec['schema']}"
                     + (f"  (filter: '{name_filter}')" if name_filter else ""))
            else:
                _warn(f"No tables found in {rec['database']}.{rec['schema']}"
                      + (f" matching '{name_filter}'" if name_filter else ""))
        except Exception as exc:
            _err(f"Failed to list tables: {exc}")

    # Snowflake
    sf_db     = os.getenv("SNOWFLAKE_DATABASE", "")
    sf_schema = os.getenv("SNOWFLAKE_SCHEMA", "")
    print(f"\n  {_C.BOLD}{_C.BLUE}TARGET  Snowflake{_C.RESET}  —  {sf_db}.{sf_schema}")
    if sf_db:
        try:
            from sql_extractor import SnowflakeExtractor
            tables = SnowflakeExtractor().list_tables(sf_schema)
            if name_filter:
                tables = [t for t in tables if name_filter.lower() in t.lower()]
            if tables:
                cols = 3
                for i in range(0, len(tables), cols):
                    row = tables[i:i+cols]
                    print("    " + "  ".join(f"{_C.CYAN}•{_C.RESET} {t:<30}" for t in row))
                _dim(f"  {len(tables)} table(s) in {sf_db}.{sf_schema}"
                     + (f"  (filter: '{name_filter}')" if name_filter else ""))
            else:
                _warn(f"No tables found in {sf_db}.{sf_schema}")
        except Exception as exc:
            _err(f"Snowflake list failed: {exc}")
    else:
        _warn("Snowflake not configured.")

    print()


# ─────────────────────────────────────────────────────────────────────────────
# INTERACTIVE MENU (no command given)
# ─────────────────────────────────────────────────────────────────────────────

def _env_status_block() -> str:
    """Return a multi-line status block showing all configured connections."""
    env_path = _SRC_DIR.parent / ".env"
    if not env_path.exists():
        return f"  {_C.YELLOW}⚠  No .env found — run [0] Setup Wizard first{_C.RESET}"

    try:
        from setup_wizard import print_connection_registry
        registry = print_connection_registry(env_path)
    except Exception:
        registry = []

    dial_key   = os.getenv("DIAL_API_KEY", "")
    claude_key = os.getenv("CLAUDE_API_KEY", "")
    sf_account = os.getenv("SNOWFLAKE_ACCOUNT", "")

    # Determine active backend label for status bar
    if dial_key:
        ai_label = f"DIAL: {os.getenv('DIAL_MODEL', 'gpt-4o')}"
        ai_icon  = f"{_C.GREEN}✓{_C.RESET}"
    elif claude_key:
        ai_label = f"Claude: {os.getenv('CLAUDE_MODEL', 'claude-3-5-sonnet-20241022')}"
        ai_icon  = f"{_C.CYAN}✓{_C.RESET}"
    else:
        ai_label = "no API key configured"
        ai_icon  = f"{_C.YELLOW}⚠{_C.RESET}"

    lines = [f"  {_C.GREEN}✓{_C.RESET}  .env loaded"]

    if registry:
        for rec in registry:
            lines.append(
                f"  {_C.CYAN}SRC_{rec['index']}{_C.RESET}"
                f"  {rec['db_label']:<14}"
                f"  {rec['host']}:{rec['port']}/{rec['database']}.{rec['schema']}"
            )
    else:
        lines.append(f"  {_C.YELLOW}⚠{_C.RESET}  No source connections — run [0] Setup Wizard")

    sf_icon = f"{_C.GREEN}✓{_C.RESET}" if sf_account else f"{_C.YELLOW}⚠{_C.RESET}"
    lines.append(
        f"  {sf_icon}  Snowflake: {sf_account or 'not configured'}   "
        f"{ai_icon}  AI: {ai_label}"
    )
    return "\n".join(lines)


def cmd_interactive():
    """Show interactive top-level menu when no command is given."""
    from rule_book import rule_book

    _banner()
    stats = rule_book.stats()
    print(_env_status_block())
    print(f"\n  {_C.DIM}Rule Book : {stats['base_rules']} base + {stats['learned_rules']} learned = {stats['total_rules']} total rules{_C.RESET}")

    # Badge: count user global exclusions across all 3 source-type files
    user_excl_count = sum(
        len(_load_global_user_exclusions(t)) for t in ("postgresql", "mssql", "athena")
    )
    excl_badge   = f"  {_C.DIM}({user_excl_count} saved){_C.RESET}" if user_excl_count else ""

    # Badge: show current AI model and key status
    # IMPORTANT: Claude backend uses CLAUDE_MODEL, not DIAL_MODEL
    dial_key      = os.getenv("DIAL_API_KEY", "")
    claude_key    = os.getenv("CLAUDE_API_KEY", "")
    if dial_key:
        current_model = os.getenv("DIAL_MODEL", "gpt-4o")
        ai_badge = f"  {_C.DIM}(DIAL: {current_model}){_C.RESET}"
    elif claude_key:
        current_model = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
        ai_badge = f"  {_C.DIM}(Claude direct: {current_model}){_C.RESET}"
    else:
        current_model = os.getenv("DIAL_MODEL", "gpt-4o")
        ai_badge = f"  {_C.YELLOW}(no API key — set DIAL_API_KEY or CLAUDE_API_KEY){_C.RESET}"

    print(f"""
  {_C.BOLD}What would you like to do?{_C.RESET}

  ── Diagnostics ──────────────────────────────────────────────────
    {_C.MAGENTA}[c]{_C.RESET}  Connections        ← ping PostgreSQL / MS SQL Server / Athena / Snowflake

  ── Validation Workflows ─────────────────────────────────────────
    {_C.GREEN}[1]{_C.RESET} Run Multiple Tables         ← pick source → pick schema → type table names → validate all
                     (type just one table name to validate a single table)

  ── Exclusion Management ─────────────────────────────────────────
    {_C.YELLOW}[E]{_C.RESET}  Global Exclusions  ← add columns excluded from ALL tables, source & target"""
    + excl_badge +
    f"""

  ── Tools ────────────────────────────────────────────────────────
    {_C.CYAN}[2]{_C.RESET}  List tables        ← show tables in all configured databases
    {_C.CYAN}[3]{_C.RESET}  Select AI model"""
    + ai_badge +
    f"""
    {_C.CYAN}[4]{_C.RESET}  View rule book
    {_C.CYAN}[5]{_C.RESET}  Add custom rule
    {_C.CYAN}[6]{_C.RESET}  List available AI models
    {_C.CYAN}[7]{_C.RESET}  Configure API key  ← set/change DIAL or Claude API key in .env
    {_C.DIM}[q]{_C.RESET}  Quit
""")

    choice = input("  Enter choice: ").strip().lower()
    ns = argparse.Namespace()

    if choice in ("c", "conn", "connections"):
        cmd_connections(ns)
    elif choice in ("1", "multiple", "tables", "run"):
        cmd_multi_db(ns)
    elif choice in ("2", "list-tables", "list"):
        cmd_list_tables(ns)
    elif choice in ("3", "model", "select-model"):
        # Read the correct current model depending on active backend
        _dk = os.getenv("DIAL_API_KEY", "")
        _ck = os.getenv("CLAUDE_API_KEY", "")
        if _ck and not _dk:
            current_model = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
        else:
            current_model = os.getenv("DIAL_MODEL", "gpt-4o")
        new_model = _select_model_interactive(current_model)
        # Persist to the correct env var
        if _ck and not _dk:
            os.environ["CLAUDE_MODEL"] = new_model
            _ok(f"Claude model set to '{new_model}' for this session.")
            _dim("To persist: update CLAUDE_MODEL in your .env file.")
        else:
            os.environ["DIAL_MODEL"] = new_model
            _ok(f"DIAL model set to '{new_model}' for this session.")
            _dim("To persist: update DIAL_MODEL in your .env file.")
    elif choice in ("4", "rules"):
        cmd_rules(ns)
    elif choice in ("5", "add-rule", "add"):
        cmd_add_rule(ns)
    elif choice in ("6", "list-models", "models"):
        _list_models_cmd()
    elif choice in ("e", "exclusion", "exclusions", "add-exclusion"):
        cmd_add_exclusion(ns)
    elif choice in ("7", "api-key", "configure-api", "apikey"):
        cmd_configure_api_key(ns)
    elif choice in ("q", "quit", "exit"):
        _dim("Bye!")
    else:
        _warn(f"Unknown choice '{choice}'. Please enter 1-7, c, e, or q.")


def _pick_source_connection() -> Optional[dict]:
    """
    Two-step source picker:
      Step 1 — choose DB type (PostgreSQL / MS SQL Server / Athena / Snowflake)
      Step 2 — if multiple connections of that type exist, pick the specific slot.
    Returns the registry dict entry, or None if user cancels.
    """
    from setup_wizard import print_connection_registry
    env_path = _SRC_DIR.parent / ".env"
    registry = print_connection_registry(env_path)

    if not registry:
        _err("No source connections configured. Run:  python validate_cli.py setup")
        return None

    # Normalize db_type for every record so downstream code is consistent
    for rec in registry:
        rec["db_type"] = _normalize_db_type(rec["db_type"])

    if len(registry) == 1:
        rec = registry[0]
        _ok(f"Using {rec['db_label']}  {rec['host']}:{rec['port']}/{rec['database']}.{rec['schema']}")
        return rec

    # ── Step 1: group by DB type ──────────────────────────────────────────────
    # Build ordered list of unique types present in the registry
    seen_types = []
    for rec in registry:
        t = rec["db_type"]
        if t not in seen_types:
            seen_types.append(t)

    _head("SELECT DATABASE SERVER")
    print(f"\n  {_C.BOLD}Which database server do you want to validate?{_C.RESET}\n")
    for i, t in enumerate(seen_types, 1):
        label = _DB_TYPE_LABELS.get(t, t)
        count = sum(1 for r in registry if r["db_type"] == t)
        slots = f"  {_C.DIM}({count} connection{'s' if count > 1 else ''}){_C.RESET}" if count > 1 else ""
        print(f"    {_C.CYAN}[{i}]{_C.RESET}  {label}{slots}")
    print(f"\n    {_C.DIM}[q]{_C.RESET}  Cancel\n")

    chosen_type = None
    while True:
        raw = input("  Enter number: ").strip().lower()
        if raw in ("q", "quit", "cancel", ""):
            return None
        try:
            n = int(raw)
            if 1 <= n <= len(seen_types):
                chosen_type = seen_types[n - 1]
                break
        except ValueError:
            pass
        _warn(f"Please enter 1–{len(seen_types)} or q to cancel.")

    # ── Step 2: if multiple slots of same type, pick one ─────────────────────
    candidates = [r for r in registry if r["db_type"] == chosen_type]
    if len(candidates) == 1:
        rec = candidates[0]
        _ok(f"Using {rec['db_label']}  {rec['host']}:{rec['port']}/{rec['database']}.{rec['schema']}")
        return rec

    print(f"\n  {_C.BOLD}Multiple {_DB_TYPE_LABELS.get(chosen_type, chosen_type)} connections:{_C.RESET}\n")
    for rec in candidates:
        print(
            f"    {_C.CYAN}[{rec['index']}]{_C.RESET}  {rec['host']}:{rec['port']}"
            f"/{rec['database']}.{rec['schema']}"
        )
    print()

    indices = [r["index"] for r in candidates]
    while True:
        raw = input("  Pick connection number: ").strip()
        try:
            n = int(raw)
            matches = [r for r in candidates if r["index"] == n]
            if matches:
                return matches[0]
        except ValueError:
            pass
        _warn(f"Please enter one of: {indices}")


def _pick_table_from_source(rec: dict) -> str:
    """
    List tables in the chosen source connection and let the user pick one.
    Returns the selected table name, or empty string if user types it manually.
    """
    from sql_extractor import ExtractorFactory
    from setup_wizard import DB_TYPES

    _head(f"TABLES IN  {rec['database']}.{rec['schema']}")
    print(f"  {_C.DIM}Fetching table list from {rec['db_label']} ...{_C.RESET}")

    tables = []
    db_type = _normalize_db_type(rec["db_type"])
    try:
        extractor = ExtractorFactory.create(
            db_type,
            host=rec["host"],
            port=int(rec.get("port") or DB_TYPES.get(db_type, ("", 5432))[1]),
            database=rec["database"],
            username=rec["username"],
            password=os.getenv(f"{rec['prefix']}PASSWORD", ""),
            auth=rec.get("auth", ""),
        )
        tables = extractor.list_tables(rec["schema"])
    except Exception as exc:
        _warn(f"Could not list tables: {exc}")
        _dim("You can still type the table name manually below.")

    if tables:
        # Display in columns
        col_w = 32
        cols  = max(1, 70 // col_w)
        print()
        for i in range(0, len(tables), cols):
            row = tables[i:i+cols]
            line = "  " + "  ".join(
                f"{_C.DIM}[{i+j+1:>3}]{_C.RESET} {_C.GREEN}{t:<{col_w}}{_C.RESET}"
                for j, t in enumerate(row)
            )
            print(line)
        print(f"\n  {_C.DIM}{len(tables)} table(s) found{_C.RESET}")
        print(f"\n  {_C.DIM}Enter number to select, or type a table name directly:{_C.RESET}")

        raw = input("  Table: ").strip()
        try:
            n = int(raw)
            if 1 <= n <= len(tables):
                return tables[n - 1]
        except ValueError:
            pass
        return raw  # user typed a name
    else:
        return _prompt("Table name", "").strip()


def _pick_snowflake_target() -> tuple:
    """
    Select Snowflake account, database, and schema.
    When all credentials are already in .env, bypass interactive prompts and use
    them directly. Only asks interactively for fields that are missing.
    Returns (sf_database, sf_schema) and propagates into os.environ.
    """
    _head("SELECT SNOWFLAKE TARGET")

    # ── Step 1: credentials (default from env) ────────────────────────────────
    default_account  = os.getenv("SNOWFLAKE_ACCOUNT",   "")
    default_username = os.getenv("SNOWFLAKE_USERNAME",  "")
    default_password = os.getenv("SNOWFLAKE_PASSWORD",  "")
    default_db       = os.getenv("SNOWFLAKE_DATABASE",  "")
    default_schema   = os.getenv("SNOWFLAKE_SCHEMA",    "")
    sf_warehouse     = os.getenv("SNOWFLAKE_WAREHOUSE", "")
    sf_role          = os.getenv("SNOWFLAKE_ROLE", "")

    # Bypass credential prompts when .env already has everything
    if default_account and default_username and default_password:
        sf_account  = default_account
        sf_username = default_username
        sf_password = default_password
        _ok(f"Using Snowflake credentials from .env  ({sf_account})")
    else:
        print(f"  {_C.DIM}Current .env account : {default_account}{_C.RESET}")
        sf_account  = _prompt("Snowflake account",  default_account ).strip() or default_account
        sf_username = _prompt("Snowflake username", default_username).strip() or default_username
        sf_password = _prompt("Snowflake password", default_password).strip() or default_password

    # ── Step 2: live database discovery ──────────────────────────────────────
    databases = []
    if sf_account and sf_username and sf_password:
        try:
            _dim("Connecting to Snowflake to list databases …")
            import snowflake.connector
            params = dict(account=sf_account, user=sf_username, password=sf_password,
                          login_timeout=20)
            if sf_warehouse:
                params["warehouse"] = sf_warehouse
            if sf_role:
                params["role"] = sf_role
            _conn = snowflake.connector.connect(**params)
            _cur  = _conn.cursor()
            _cur.execute("SHOW DATABASES;")
            databases = [row[1] for row in _cur.fetchall()]
            _conn.close()
            _ok(f"Found {len(databases)} databases")
        except Exception as exc:
            _warn(f"Could not list Snowflake databases: {exc}")

    if databases:
        print()
        for i, db in enumerate(databases, 1):
            marker = f"  {_C.DIM}← env default{_C.RESET}" if db == default_db else ""
            print(f"    {_C.CYAN}[{i}]{_C.RESET}  {db}{marker}")
        print()
        raw = input(f"  Select database number or type name [{_C.DIM}{default_db}{_C.RESET}]: ").strip()
        if not raw:
            sf_db = default_db
        else:
            try:
                n = int(raw)
                sf_db = databases[n - 1] if 1 <= n <= len(databases) else raw
            except ValueError:
                sf_db = raw
    else:
        sf_db = _prompt("Snowflake database", default_db).strip() or default_db

    # ── Step 3: live schema discovery for chosen database ─────────────────────
    schemas = []
    if sf_db and sf_account and sf_username and sf_password:
        try:
            _dim(f"Fetching schemas from {sf_db} …")
            import snowflake.connector
            params = dict(account=sf_account, database=sf_db, user=sf_username,
                          password=sf_password, login_timeout=20)
            if sf_warehouse:
                params["warehouse"] = sf_warehouse
            if sf_role:
                params["role"] = sf_role
            _conn2 = snowflake.connector.connect(**params)
            _cur2  = _conn2.cursor()
            _cur2.execute(f"SHOW SCHEMAS IN DATABASE {sf_db};")
            schemas = [row[1] for row in _cur2.fetchall() if row[1] != "INFORMATION_SCHEMA"]
            _conn2.close()
        except Exception as exc:
            _warn(f"Could not list Snowflake schemas: {exc}")

    if schemas:
        print()
        for i, s in enumerate(schemas, 1):
            marker = f"  {_C.DIM}← env default{_C.RESET}" if s == default_schema else ""
            print(f"    {_C.CYAN}[{i}]{_C.RESET}  {s}{marker}")
        print()
        raw = input(f"  Select schema number or type name [{_C.DIM}{default_schema}{_C.RESET}]: ").strip()
        if not raw:
            sf_schema = default_schema
        else:
            try:
                n = int(raw)
                sf_schema = schemas[n - 1] if 1 <= n <= len(schemas) else raw
            except ValueError:
                sf_schema = raw
    else:
        sf_schema = _prompt("Snowflake schema", default_schema).strip() or default_schema

    # ── Step 4: propagate everything into os.environ ──────────────────────────
    # This is the critical step — every downstream SnowflakeExtractor() that
    # reads from os.getenv() will now see the interactively chosen values.
    os.environ["SNOWFLAKE_ACCOUNT"]  = sf_account
    os.environ["SNOWFLAKE_USERNAME"] = sf_username
    os.environ["SNOWFLAKE_PASSWORD"] = sf_password
    os.environ["SNOWFLAKE_DATABASE"] = sf_db
    os.environ["SNOWFLAKE_SCHEMA"]   = sf_schema

    _ok(f"Snowflake target: {sf_account} / {sf_db}.{sf_schema}")
    return sf_db, sf_schema


def _pick_snowflake_table(suggested: str) -> tuple:
    """
    Full Snowflake target picker for Single Table flow.
    Step 1: call _pick_snowflake_target() so user can choose account/database/schema
            (writes selection into os.environ so all downstream callers see it).
    Step 2: list tables in the chosen database.schema, auto-match against suggested.
    Returns (sf_table, sf_schema, sf_database).
    """
    from sql_extractor import SnowflakeExtractor

    # Let user interactively choose account → database → schema.
    # This also propagates into os.environ for all subsequent extractors.
    sf_db, sf_schema = _pick_snowflake_target()

    _head("SELECT SNOWFLAKE TARGET TABLE")
    print(f"  {_C.DIM}Target: {sf_db}.{sf_schema}{_C.RESET}")

    # List tables in the chosen database/schema
    sf_tables = []
    try:
        _dim("Fetching Snowflake table list ...")
        sf_tables = SnowflakeExtractor().list_tables(sf_schema)
    except Exception as exc:
        _warn(f"Could not list Snowflake tables: {exc}")

    if sf_tables:
        import difflib
        upper_map = {t.upper(): t for t in sf_tables}
        auto_match = upper_map.get(suggested.upper(), "")

        if auto_match:
            _ok(f"Auto-matched Snowflake table: {auto_match}  {_C.DIM}(confidence: 100%){_C.RESET}")
            confirm = input(
                f"  Use {_C.GREEN}{auto_match}{_C.RESET}? [Y/n]: "
            ).strip().lower()
            if confirm not in ("n", "no"):
                return auto_match, sf_schema, sf_db

        # Show top matches with similarity scores
        sm = difflib.SequenceMatcher
        scored = sorted(
            ((t, int(sm(None, suggested.upper(), t).ratio() * 100)) for t in upper_map),
            key=lambda x: x[1], reverse=True,
        )
        close = [(t, pct) for t, pct in scored if pct >= 30][:8]
        if close:
            print(f"\n  {_C.BOLD}Closest Snowflake tables to '{suggested}':{_C.RESET}\n")
            for i, (t, pct) in enumerate(close, 1):
                bar = f"{_C.GREEN}" if pct >= 80 else (f"{_C.YELLOW}" if pct >= 50 else f"{_C.DIM}")
                print(f"    {_C.CYAN}[{i}]{_C.RESET}  {upper_map[t]:<40} {bar}{pct}%{_C.RESET}")
            print(f"    {_C.DIM}[0]  Type manually{_C.RESET}\n")
            raw = input("  Pick number or type table name: ").strip()
            try:
                n = int(raw)
                if 1 <= n <= len(close):
                    return upper_map[close[n - 1][0]], sf_schema, sf_db
                elif n == 0:
                    pass  # fall through to manual
            except ValueError:
                if raw:
                    return raw, sf_schema, sf_db

    # Manual entry
    sf_table  = _prompt("Snowflake table name", suggested.upper()).strip()
    sf_schema = _prompt("Snowflake schema", sf_schema).strip() or sf_schema
    sf_db_in  = _prompt("Snowflake database", sf_db).strip() or sf_db
    return sf_table, sf_schema, sf_db_in


def _get_connection_by_index(index: int) -> Optional[dict]:
    """Return the registry entry for SRC_<index>, or None if not found."""
    from setup_wizard import print_connection_registry
    env_path = _SRC_DIR.parent / ".env"
    registry = print_connection_registry(env_path)
    matches = [r for r in registry if r["index"] == index]
    if matches:
        return _apply_database_registry(matches[0])
    _err(f"No connection found for SRC_{index} in .env. Run: python validate_cli.py connections")
    return None


def _apply_database_registry(record: dict) -> dict:
    """Fill missing database/schema metadata without loading credentials."""
    import yaml

    fallback_path = _SRC_DIR.parent / "config" / "database_registry.yaml"
    try:
        with open(fallback_path, encoding="utf-8") as handle:
            fallback = yaml.safe_load(handle) or {}
        key = record.get("prefix", f"SRC_{record.get('index', 1)}").rstrip("_")
        metadata = fallback.get("databases", {}).get(key, {})
        record["database"] = record.get("database") or metadata.get("database", "")
        record["schema"] = record.get("schema") or metadata.get("schema", "")
    except (OSError, yaml.YAMLError):
        pass
    return record


def _override_source_env(rec: dict) -> None:
    """Override SOURCE_* env vars in-process to point at the chosen connection."""
    os.environ["SOURCE_TYPE"]     = _normalize_db_type(rec["db_type"])
    os.environ["SOURCE_HOST"]     = rec["host"]
    os.environ["SOURCE_PORT"]     = str(rec.get("port", "5432"))
    os.environ["SOURCE_DATABASE"] = rec["database"]
    os.environ["SOURCE_SCHEMA"]   = rec["schema"]
    os.environ["SOURCE_USERNAME"] = rec["username"]
    os.environ["SOURCE_AUTH"]     = rec.get("auth", "")
    src_pass = os.getenv(f"{rec['prefix']}PASSWORD", "")
    os.environ["SOURCE_PASSWORD"] = src_pass
    # Athena-specific: propagate S3 output location and region so AthenaExtractor finds them
    if _normalize_db_type(rec["db_type"]) == "athena":
        s3_out = rec.get("s3_output", "")
        if s3_out:
            os.environ["ATHENA_S3_OUTPUT"] = s3_out
        if rec.get("host"):
            os.environ["ATHENA_REGION"] = rec["host"]


def _make_source_extractor(rec: dict):
    """Build the correct BaseExtractor for the chosen source connection record."""
    from sql_extractor import ExtractorFactory
    from setup_wizard import DB_TYPES
    db_type = _normalize_db_type(rec["db_type"])
    return ExtractorFactory.create(
        db_type,
        host=rec["host"],
        port=int(rec.get("port") or DB_TYPES.get(db_type, ("", 5432))[1]),
        database=rec["database"],
        username=rec["username"],
        password=os.getenv(f"{rec['prefix']}PASSWORD", ""),
        auth=rec.get("auth", ""),
        s3_output=rec.get("s3_output", ""),
    )


def _resolve_yaml_source(source_type: str, current: dict) -> dict:
    """Resolve a YAML block to the configured source matching its DB type."""
    wanted = _normalize_db_type(source_type or current.get("type", "postgresql"))
    try:
        from setup_wizard import print_connection_registry
        registry = [_apply_database_registry(record) for record in print_connection_registry(_SRC_DIR.parent / ".env")]
        for rec in registry:
            if _normalize_db_type(rec.get("db_type", "")) == wanted:
                return {
                    "type": wanted,
                    "host": rec.get("host", ""),
                    "port": rec.get("port", "5432"),
                    "database": rec.get("database", ""),
                    "username": rec.get("username", ""),
                    "password": os.getenv(f"{rec.get('prefix', '')}PASSWORD", ""),
                    "schema": rec.get("schema", "public"),
                    "auth": rec.get("auth", ""),
                }
    except Exception:
        pass
    if _normalize_db_type(current.get("type", "")) == wanted:
        return current
    return {**current, "type": wanted}


def _select_column_exclusions(rec: dict, schema: str, table: str, initial=None) -> list:
    """
    Interactive column exclusion prompt — two options:

      Option G — GLOBAL: exclude this column from ALL tables of THIS SOURCE
                 DATABASE TYPE, in both that source and the Snowflake target.
                 Saved permanently to config/<db_type>_exclusions.yaml under
                 user_global_exclusions.

      Option T — TABLE-ONLY: exclude column only for this specific table run.
                 Not saved anywhere; applies to current session only.

    Shows every column in the table with auto-excluded columns marked [auto].
    """
    src_db_type  = _normalize_db_type((rec or {}).get("db_type", "postgresql"))
    excl_path    = _exclusions_path_for(src_db_type)
    all_excluded = _get_all_exclusions(src_db_type)      # static + user global for this source type
    selected     = list(initial or [])

    # Fetch live columns from source
    names = []
    try:
        extractor = _make_source_extractor(rec)
        columns   = extractor.extract_columns(schema, table)
        names     = [getattr(col, "column_name", str(col)) for col in columns]
    except Exception as exc:
        _warn(f"Could not load columns for {table}: {exc}")
        _dim("You can still type column names manually.")

    all_excl_set = {c.lower() for c in all_excluded}

    # ── Show column list ──────────────────────────────────────────────────────
    print(f"\n  {_C.BOLD}{_C.CYAN}{'─' * 68}{_C.RESET}")
    print(f"  {_C.BOLD}COLUMN EXCLUSION — Table: {table}{_C.RESET}")
    print(f"  {_C.DIM}Auto-excluded columns (marked [auto]) are skipped in source AND target.{_C.RESET}")
    print(f"  {_C.BOLD}{_C.CYAN}{'─' * 68}{_C.RESET}\n")

    if names:
        for idx, name in enumerate(names, 1):
            if name.lower() in all_excl_set:
                print(
                    f"    {_C.DIM}[{idx:>2}]{_C.RESET}  "
                    f"{_C.DIM}{name:<35}{_C.RESET}  "
                    f"{_C.YELLOW}[auto-excluded]{_C.RESET}"
                )
            else:
                print(f"    {_C.CYAN}[{idx:>2}]{_C.RESET}  {_C.GREEN}{name}{_C.RESET}")
    else:
        _dim("  (column list unavailable — type names manually)")

    # ── Show exclusion options ────────────────────────────────────────────────
    print(f"\n  {_C.BOLD}Exclusion Options:{_C.RESET}")
    print(f"    {_C.GREEN}[G]{_C.RESET}  Global  — exclude column(s) from ALL {_DB_TYPE_LABELS.get(src_db_type, src_db_type)} tables, source & Snowflake target")
    print(f"              Saved permanently to config/{excl_path.name}")
    print(f"    {_C.CYAN}[T]{_C.RESET}  Table   — exclude column(s) for THIS table only (this run)")
    print(f"    {_C.DIM}[N]{_C.RESET}  None    — no additional exclusions (just keep [auto] ones)")
    print()

    scope_choice = input("  Choose exclusion scope [G/T/N]: ").strip().upper()

    if scope_choice in ("", "N", "NO", "NONE"):
        return selected

    if scope_choice not in ("G", "T"):
        _warn(f"Unknown choice '{scope_choice}'. No additional exclusions applied.")
        return selected

    # ── Collect column names / numbers ────────────────────────────────────────
    print()
    if scope_choice == "G":
        print(f"  {_C.BOLD}{_C.GREEN}GLOBAL EXCLUSION{_C.RESET}  — column will be excluded from ALL tables forever.")
    else:
        print(f"  {_C.BOLD}{_C.CYAN}TABLE-ONLY EXCLUSION{_C.RESET}  — column excluded from this run only.")

    print(f"  {_C.DIM}Enter column numbers or names (comma-separated). Example:  3,7  or  discount_code,ref_id{_C.RESET}")
    raw = input("  Columns to exclude: ").strip()
    if not raw:
        _dim("  No columns entered — skipped.")
        return selected

    by_lower  = {n.lower(): n for n in names}
    new_cols  = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            idx = int(token)
            if 1 <= idx <= len(names):
                new_cols.append(names[idx - 1])
            else:
                _warn(f"  Column number {idx} out of range — skipped.")
        except ValueError:
            new_cols.append(by_lower.get(token.lower(), token))

    if not new_cols:
        return selected

    # ── Global: ask reason and persist ───────────────────────────────────────
    if scope_choice == "G":
        print()
        print(f"  {_C.BOLD}These column(s) will be excluded globally:{_C.RESET}")
        for col in new_cols:
            print(f"    {_C.YELLOW}•{_C.RESET}  {col}")
        print()
        reason = input(
            f"  {_C.BOLD}Reason for global exclusion{_C.RESET} "
            f"{_C.DIM}(e.g. 'CDC metadata', 'PII policy', 'No target equivalent'){_C.RESET}: "
        ).strip()
        if not reason:
            reason = "User-defined global exclusion"

        saved   = []
        skipped = []
        for col in new_cols:
            ok = _save_global_user_exclusion(src_db_type, col, reason)
            if ok:
                saved.append(col)
                selected.append(col)
            else:
                skipped.append(col)   # Already existed — still apply this run
                selected.append(col)

        if saved:
            _ok(
                f"Saved {len(saved)} global exclusion(s) to "
                f"config/{excl_path.name} → user_global_exclusions "
                f"(applies to {_DB_TYPE_LABELS.get(src_db_type, src_db_type)} source + Snowflake target)"
            )
            for col in saved:
                print(f"    {_C.GREEN}✓{_C.RESET}  {col}  ({reason})")
        if skipped:
            _dim(f"  Already globally excluded (re-applied this run): {', '.join(skipped)}")

    # ── Table-only: just add to current run ───────────────────────────────────
    else:
        for col in new_cols:
            selected.append(col)
        _ok(f"Table-only exclusion applied for this run: {', '.join(new_cols)}")
        _dim("  These columns are NOT saved — exclusion applies to this table only.")

    # Dedup while preserving order
    deduped = []
    seen    = set()
    for name in selected:
        if name.lower() not in seen:
            seen.add(name.lower())
            deduped.append(name)
    return deduped


def cmd_configure_api_key(args):
    """
    Wizard: configure AI API key in .env

    Supports two modes:
      1. EPAM DIAL  (recommended for EPAM employees)
         Uses DIAL_API_KEY + DIAL_API_BASE — proxies to any model (GPT, Claude, Gemini, etc.)
         One key, many models, on EPAM VPN.

      2. Claude Direct  (Anthropic API — no DIAL/VPN needed)
         Uses CLAUDE_API_KEY — calls Anthropic API directly.
         Limited to Claude models only.
    """
    _banner()
    _head("\U0001f511  CONFIGURE AI API KEY")

    env_path = _SRC_DIR.parent / ".env"

    # Read existing .env
    env_lines = []
    if env_path.exists():
        env_lines = env_path.read_text(encoding="utf-8").splitlines()

    def _get_env_val(key: str) -> str:
        for line in env_lines:
            if line.startswith(f"{key}="):
                return line[len(key) + 1:].strip()
        return os.getenv(key, "")

    dial_key      = _get_env_val("DIAL_API_KEY")
    dial_base     = _get_env_val("DIAL_API_BASE") or "https://ai-proxy.lab.epam.com"
    claude_key    = _get_env_val("CLAUDE_API_KEY")
    current_model = _get_env_val("DIAL_MODEL") or "gpt-4o"

    # Show current state
    dial_status   = f"{_C.GREEN}*** configured ***{_C.RESET}" if dial_key   else f"{_C.YELLOW}NOT SET{_C.RESET}"
    claude_status = f"{_C.GREEN}*** configured ***{_C.RESET}" if claude_key else f"{_C.YELLOW}NOT SET{_C.RESET}"

    print(f"""
  {_C.BOLD}Current AI Configuration:{_C.RESET}
    DIAL_API_KEY   : {dial_status}
    CLAUDE_API_KEY : {claude_status}
    DIAL_API_BASE  : {_C.DIM}{dial_base}{_C.RESET}
    DIAL_MODEL     : {_C.CYAN}{current_model}{_C.RESET}

  {_C.BOLD}Which API do you want to configure?{_C.RESET}

    {_C.GREEN}[1]{_C.RESET}  EPAM DIAL         ← recommended for EPAM employees
              One API key → access to GPT-4o, Claude, Gemini, Llama, Mistral
              Requires EPAM VPN: https://ai-proxy.lab.epam.com

    {_C.CYAN}[2]{_C.RESET}  Claude Direct     ← Anthropic API (no VPN/DIAL needed)
              Claude models only → claude-3-5-sonnet, claude-3-opus, etc.
              Get key at: https://console.anthropic.com

    {_C.DIM}[3]{_C.RESET}  Change DIAL model  ← set default model in .env
    {_C.DIM}[s]{_C.RESET}  Show current config
    {_C.DIM}[q]{_C.RESET}  Cancel
""")

    choice = input("  Choice [1/2/3/s/q]: ").strip().lower()

    if choice in ("q", "cancel", ""):
        _dim("Cancelled.")
        return

    # ── Option S: Show full config ─────────────────────────────────────────
    if choice in ("s", "show"):
        print(f"\n  {_C.BOLD}Full AI Config in .env:{_C.RESET}")
        sensitive_keys = {"DIAL_API_KEY", "CLAUDE_API_KEY"}
        ai_keys = {"DIAL_API_KEY", "DIAL_API_BASE", "DIAL_API_VERSION",
                   "DIAL_MODEL", "CLAUDE_API_KEY", "CLAUDE_MODEL"}
        for line in env_lines:
            key_part = line.split("=")[0].strip() if "=" in line else ""
            if key_part in ai_keys:
                val = line.split("=", 1)[1].strip() if "=" in line else ""
                if key_part in sensitive_keys and len(val) > 8:
                    val = val[:4] + "***" + val[-4:]  # mask the middle
                print(f"    {_C.CYAN}{key_part}{_C.RESET} = {val}")
        print()
        return

    # ── Option 3: Change DIAL model ────────────────────────────────────────
    if choice == "3":
        print(f"\n  {_C.BOLD}Available models via DIAL:{_C.RESET}")
        from ai_transformation.ai_rule_mapper import AVAILABLE_MODELS, MODEL_DESCRIPTIONS
        for i, model in enumerate(AVAILABLE_MODELS, 1):
            info   = MODEL_DESCRIPTIONS.get(model, ("Other", model, ""))
            marker = f"  {_C.GREEN}<-- current{_C.RESET}" if model == current_model else ""
            print(
                f"    {_C.DIM}[{i}]{_C.RESET}  {_C.YELLOW}{info[0]:<12}{_C.RESET}"
                f"{_C.CYAN}{model:<38}{_C.RESET}  {_C.DIM}{info[2]}{_C.RESET}{marker}"
            )
        print()
        raw = input(f"  Enter number or model name [{current_model}]: ").strip()
        if not raw:
            _dim("No change.")
            return
        try:
            idx = int(raw) - 1
            new_model = AVAILABLE_MODELS[idx] if 0 <= idx < len(AVAILABLE_MODELS) else raw
        except ValueError:
            new_model = raw
        _update_env_file(env_path, env_lines, "DIAL_MODEL", new_model)
        _ok(f"DIAL_MODEL set to '{new_model}' in .env")
        os.environ["DIAL_MODEL"] = new_model
        return

    # ── Option 1: EPAM DIAL ────────────────────────────────────────────────
    if choice == "1":
        print(f"""
  {_C.BOLD}{_C.GREEN}EPAM DIAL API Key Setup{_C.RESET}
  {_C.DIM}Get your key at: https://ai-proxy.lab.epam.com  (EPAM VPN required)
  Your key grants access to: GPT-4o, Claude Sonnet, Gemini, Llama, Mistral

  Key format: sk-... or a long alphanumeric string
  {_C.RESET}""")
        new_key = input("  Paste your DIAL_API_KEY: ").strip()
        if not new_key:
            _warn("No key entered. Cancelled.")
            return

        print(f"\n  {_C.DIM}DIAL_API_BASE default: {dial_base}{_C.RESET}")
        new_base = input(f"  DIAL_API_BASE (press Enter to keep default): ").strip() or dial_base

        print(f"\n  {_C.DIM}Recommended: gpt-4o (best) | gpt-4o-mini (fast) | anthropic.claude-sonnet-5{_C.RESET}")
        new_model = input(f"  DIAL_MODEL (press Enter to keep '{current_model}'): ").strip() or current_model

        # Preview
        masked = new_key[:4] + "***" + new_key[-4:] if len(new_key) > 8 else "***"
        print(f"\n  {_C.BOLD}Will write to .env:{_C.RESET}")
        print(f"    DIAL_API_KEY   = {masked}")
        print(f"    DIAL_API_BASE  = {new_base}")
        print(f"    DIAL_MODEL     = {new_model}")
        print()
        if input("  Save to .env? [Y/n]: ").strip().lower() in ("n", "no"):
            _dim("Cancelled.")
            return

        _update_env_file(env_path, env_lines, "DIAL_API_KEY",     new_key)
        _update_env_file(env_path, env_lines, "DIAL_API_BASE",    new_base)
        _update_env_file(env_path, env_lines, "DIAL_API_VERSION", "2025-04-01-preview")
        _update_env_file(env_path, env_lines, "DIAL_MODEL",       new_model)
        os.environ["DIAL_API_KEY"]  = new_key
        os.environ["DIAL_API_BASE"] = new_base
        os.environ["DIAL_MODEL"]    = new_model
        _ok("EPAM DIAL API key saved to .env \u2713")
        _ok(f"Model set to '{new_model}' \u2713")
        _dim(".env is git-ignored. Your key is safe.")
        return

    # ── Option 2: Claude Direct ────────────────────────────────────────────
    if choice == "2":
        print(f"""
  {_C.BOLD}{_C.CYAN}Anthropic Claude Direct API Key Setup{_C.RESET}
  {_C.DIM}Get your key at: https://console.anthropic.com  (no VPN needed)
  Available models: claude-3-5-sonnet, claude-3-opus, claude-3-haiku

  Note: CLAUDE_API_KEY is used ONLY when DIAL_API_KEY is NOT set.
        If both are set, DIAL_API_KEY (EPAM DIAL) takes priority.
  {_C.RESET}""")
        new_key = input("  Paste your CLAUDE_API_KEY (sk-ant-...): ").strip()
        if not new_key:
            _warn("No key entered. Cancelled.")
            return
        if not new_key.startswith("sk-ant-"):
            _warn("Claude keys usually start with 'sk-ant-api03-...'. Please verify.")

        claude_models = [
            "claude-3-5-sonnet-20241022",
            "claude-3-opus-20240229",
            "claude-3-haiku-20240307",
            "claude-3-5-haiku-20241022",
        ]
        print(f"\n  {_C.BOLD}Choose default Claude model:{_C.RESET}")
        for i, m in enumerate(claude_models, 1):
            print(f"    {_C.CYAN}[{i}]{_C.RESET}  {m}")
        print()
        raw_model = input("  Model number [1 = claude-3-5-sonnet]: ").strip()
        try:
            midx = int(raw_model) - 1
            claude_model = claude_models[midx] if 0 <= midx < len(claude_models) else claude_models[0]
        except (ValueError, IndexError):
            claude_model = claude_models[0]

        masked = new_key[:8] + "***" + new_key[-4:] if len(new_key) > 12 else "***"
        print(f"\n  {_C.BOLD}Will write to .env:{_C.RESET}")
        print(f"    CLAUDE_API_KEY = {masked}")
        print(f"    CLAUDE_MODEL   = {claude_model}")
        print()
        if input("  Save to .env? [Y/n]: ").strip().lower() in ("n", "no"):
            _dim("Cancelled.")
            return

        _update_env_file(env_path, env_lines, "CLAUDE_API_KEY", new_key)
        _update_env_file(env_path, env_lines, "CLAUDE_MODEL",   claude_model)
        os.environ["CLAUDE_API_KEY"] = new_key
        os.environ["CLAUDE_MODEL"]   = claude_model
        _ok("Claude API key saved to .env \u2713")
        _ok(f"Default Claude model: {claude_model} \u2713")
        _dim("DIAL_API_KEY takes priority if set. Remove it to use Claude directly.")
        _dim(".env is git-ignored. Your key is safe.")
        return

    _warn(f"Unknown choice '{choice}'. Cancelled.")


def _update_env_file(env_path, env_lines: list, key: str, value: str) -> None:
    """
    Update or append a KEY=VALUE line in the .env file.
    Reads the current file, replaces the key if found, appends if not.
    """
    updated   = False
    new_lines = []
    for line in env_lines:
        stripped = line.split("=")[0].strip() if "=" in line else line
        if stripped == key:
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    # Reflect changes back into env_lines so subsequent calls in same session see it
    env_lines.clear()
    env_lines.extend(new_lines)


def cmd_add_exclusion(args):
    """
    Wizard: add a global column exclusion to config/exclusions.yaml.

    Global exclusions are applied:
      - In BOTH source and target tables
      - Across ALL databases (PostgreSQL, MSSQL, Athena, Snowflake)
      - On EVERY validation run — permanently

    Use this for columns like CDC metadata, PII columns, or any column
    that should never be validated regardless of the table.
    """
    _banner()
    _head("🚫  ADD GLOBAL COLUMN EXCLUSION")

    print(f"""
  {_C.BOLD}What is a Global Exclusion?{_C.RESET}
  {_C.DIM}A column added here is excluded from validation for ONE source
  database type, in BOTH that source and the Snowflake target:
    • PostgreSQL  → saved to config/postgresql_exclusions.yaml
    • MSSQL       → saved to config/mssql_exclusions.yaml
    • Athena      → saved to config/athena_exclusions.yaml
    • EVERY run of that source type — saved permanently

  Use this for:
    • CDC / ETL metadata columns   (etl_batch_id, load_ts, dw_insert_dt)
    • Fivetran system columns      (_fivetran_synced, _fivetran_deleted)
    • PII / sensitive columns      (social_security_no, credit_card_no)
    • Audit columns added post-migration (migrated_by, migrated_at)
    • Columns with no target equivalent in any table

  You can also exclude columns PER TABLE ONLY during the generate workflow.
  {_C.RESET}""")

    # ── Ask which source database type this exclusion applies to ────────────
    print(f"  {_C.BOLD}Which source database does this apply to?{_C.RESET}")
    print(f"    {_C.CYAN}[1]{_C.RESET}  PostgreSQL")
    print(f"    {_C.CYAN}[2]{_C.RESET}  MSSQL")
    print(f"    {_C.CYAN}[3]{_C.RESET}  Athena")
    print(f"    {_C.CYAN}[4]{_C.RESET}  All three")
    db_choice = input("  Choice [1-4]: ").strip()
    db_type_map = {"1": ["postgresql"], "2": ["mssql"], "3": ["athena"],
                   "4": ["postgresql", "mssql", "athena"]}
    target_db_types = db_type_map.get(db_choice)
    if not target_db_types:
        _warn(f"Unknown choice '{db_choice}'. Cancelled.")
        return

    static = [c.lower() for c in STATIC_EXCLUDE_COLUMNS]
    print(f"\n  {_C.BOLD}Currently auto-excluded (built-in, {len(static)} columns):{_C.RESET}")
    for col in STATIC_EXCLUDE_COLUMNS:
        print(f"    {_C.DIM}• {col}{_C.RESET}")

    # ── Show existing global exclusions for the chosen source type(s) ───────
    for db_type in target_db_types:
        existing = _load_global_user_exclusions(db_type)
        excl_path = _exclusions_path_for(db_type)
        label = _DB_TYPE_LABELS.get(db_type, db_type)
        if existing:
            print(f"\n  {_C.BOLD}{label} — saved global exclusions ({len(existing)} columns):{_C.RESET}")
            import yaml
            try:
                with open(excl_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                for entry in data.get("user_global_exclusions", []):
                    col    = entry.get("column_name", "")
                    reason = entry.get("reason", "")
                    added  = entry.get("date_added", "")
                    print(f"    {_C.YELLOW}•{_C.RESET}  {col:<35} {_C.DIM}{reason}  [{added}]{_C.RESET}")
            except Exception:
                for col in existing:
                    print(f"    {_C.YELLOW}•{_C.RESET}  {col}")
        else:
            print(f"\n  {_C.DIM}{label}: no user-defined global exclusions yet.{_C.RESET}")

    print()
    _sep()

    # ── Collect new exclusion(s) ──────────────────────────────────────────────
    print(f"\n  {_C.BOLD}Add New Global Exclusion(s){_C.RESET}")
    print(f"  {_C.DIM}Enter one or more column names separated by commas.{_C.RESET}")
    raw = input("  Column name(s): ").strip()
    if not raw:
        _warn("No column name entered. Cancelled.")
        return

    columns = [c.strip() for c in raw.split(",") if c.strip()]
    if not columns:
        _warn("No valid column names found. Cancelled.")
        return

    reason = input(
        f"  {_C.BOLD}Reason{_C.RESET} "
        f"{_C.DIM}(e.g. 'CDC metadata not migrated', 'PII column — excluded by policy'){_C.RESET}: "
    ).strip()
    if not reason:
        reason = "User-defined global exclusion"

    # ── Preview ───────────────────────────────────────────────────────────────
    target_labels = ", ".join(_DB_TYPE_LABELS.get(t, t) for t in target_db_types)
    print(f"\n  {_C.BOLD}Preview — will be excluded from ALL {target_labels} tables, source + Snowflake target:{_C.RESET}")
    for col in columns:
        print(f"    {_C.YELLOW}•{_C.RESET}  {col}  —  {_C.DIM}{reason}{_C.RESET}")
    print()

    confirm = input("  Save these global exclusions? [Y/n]: ").strip().lower()
    if confirm in ("n", "no"):
        _dim("Cancelled.")
        return

    for db_type in target_db_types:
        excl_path = _exclusions_path_for(db_type)
        label = _DB_TYPE_LABELS.get(db_type, db_type)
        saved_count   = 0
        already_count = 0
        for col in columns:
            ok = _save_global_user_exclusion(db_type, col, reason)
            if ok:
                saved_count += 1
                _ok(f"[{label}] Saved: '{col}' — excluded from {label} source + Snowflake target")
            else:
                already_count += 1
                _dim(f"  [{label}] Already exists: '{col}' (skipped)")

        if saved_count:
            _ok(
                f"[{label}] {saved_count} global exclusion(s) saved to "
                f"config/{excl_path.name} → user_global_exclusions"
            )
        if already_count:
            _dim(f"  [{label}] {already_count} column(s) already excluded — no change.")

    print(f"\n  {_C.DIM}These exclusions take effect on the next validation run.{_C.RESET}")
    print()


def _canonical_validation_value(column_name: str, value):
    """Normalize equivalent JSON/HStore representations before comparison.

    Delegates to Project/utils/semantic_normalize.py, which is the single
    implementation shared with Project/main.py — the runtime comparison engine.

    The previous inline version stayed behind it in three ways, all of which
    produced false mismatches on real data:
      * it dispatched on the column *name* ("json" in column_name), so a
        semi-structured column named anything else was skipped entirely;
      * it did not recurse into values that are themselves serialized JSON
        documents (the common hstore case), so a re-serialized inner document
        read as data drift;
      * its hstore regex — r'"([^"]+)"\\s*=>\\s*"([^"]*)"' — truncated any value
        containing an escaped quote, e.g.
        "payables"=>"[{\\"ledger_id\\":6752258}]".
    """
    if value is None:
        return None
    return canonicalize_value(str(value).strip())


def _run_parameterized_tables(args, current_model: str, exclude_cols: list) -> None:
    """
    Parameterized multi-table flow:
      --source N --tables t1,t2,t3 [--exclude col1,col2]

    Selects one source connection, then runs the pipeline for each table
    in the comma-separated list, auto-matching Snowflake tables via difflib.
    All results are collected and summarised at the end.

    This is distinct from batch mode (which reads YAML). Here table names
    are provided as inline CLI parameters.
    """
    from validation_pipeline import ValidationPipeline
    from sql_extractor import SnowflakeExtractor

    _head("📋  PARAMETERIZED MULTI-TABLE VALIDATION")

    tables_raw   = getattr(args, "tables", "") or ""
    source_index = getattr(args, "source_index", None)
    pg_schema    = getattr(args, "source_schema",   None)
    pg_database  = getattr(args, "source_database", None)
    sf_schema    = getattr(args, "sf_schema",   None)
    sf_database  = getattr(args, "sf_database", None)

    source_tables = [t.strip() for t in tables_raw.split(",") if t.strip()]
    if not source_tables:
        _err("--tables flag was empty. Provide comma-separated table names.")
        return

    # ── Select connection ─────────────────────────────────────────────────────
    if source_index is not None:
        rec = _get_connection_by_index(source_index)
    else:
        rec = _pick_source_connection()

    if rec is None:
        return

    pg_schema   = pg_schema   or rec["schema"]
    pg_database = pg_database or rec["database"]
    _override_source_env(rec)

    if not sf_database or not sf_schema:
        sf_database, sf_schema = _pick_snowflake_target()
    sf_schema   = sf_schema   or os.getenv("SNOWFLAKE_SCHEMA",   "")
    sf_database = sf_database or os.getenv("SNOWFLAKE_DATABASE", "")

    _ok(f"Source : SRC_{rec['index']}  {rec['db_label']}  {rec['host']}:{rec['port']}/{pg_database}.{pg_schema}")
    _ok(f"Target : Snowflake  {sf_database}.{sf_schema}")
    if exclude_cols:
        _warn(f"Excluding columns: {', '.join(exclude_cols)}")

    print(f"\n  {_C.BOLD}Tables to process ({len(source_tables)}):{_C.RESET}")
    for i, t in enumerate(source_tables, 1):
        print(f"    {_C.CYAN}[{i}]{_C.RESET}  {t}")
    print()

    # ── Pre-load Snowflake table list for auto-match ──────────────────────────
    sf_tables = []
    try:
        _dim("Fetching Snowflake table list for auto-match ...")
        sf_tables = SnowflakeExtractor(database=sf_database).list_tables(sf_schema)
        _ok(f"Found {len(sf_tables)} Snowflake tables in {sf_database}.{sf_schema}")
    except Exception as exc:
        _warn(f"Could not list Snowflake tables: {exc}")
        _dim("You will be asked for each Snowflake table name manually.")
    print()

    upper_sf = {t.upper(): t for t in sf_tables}

    import difflib

    # ── Confirm before running ────────────────────────────────────────────────
    confirm = input(
        f"  Run validation for all {len(source_tables)} table(s)? [Y/n]: "
    ).strip().lower()
    if confirm in ("n", "no"):
        _dim("Cancelled.")
        return

    # ── Layer selection ───────────────────────────────────────────────────────
    print(f"\n  {_C.BOLD}Select data layer:{_C.RESET}")
    print(f"    {_C.CYAN}[1]{_C.RESET}  bronze  (default)")
    print(f"    {_C.CYAN}[2]{_C.RESET}  silver")
    print(f"    {_C.CYAN}[3]{_C.RESET}  gold")
    _layer_choice = input("  Layer [1]: ").strip() or "1"
    _layer = {"1": "bronze", "2": "silver", "3": "gold"}.get(_layer_choice, "bronze")
    _output_dir = Path(__file__).parent.parent / "Project" / "config" / _layer

    # ── Pipeline ──────────────────────────────────────────────────────────────
    src_extractor = _make_source_extractor(rec)
    pipeline   = ValidationPipeline(model=current_model, source_extractor=src_extractor)
    results    = []   # list of (src_table, sf_table, result | exception)
    succeeded  = 0
    failed     = 0

    for i, src_table in enumerate(source_tables, 1):
        print()
        _sep("═")
        print(
            f"  {_C.BOLD}{_C.MAGENTA}TABLE {i}/{len(source_tables)}{_C.RESET}  "
            f"{_C.GREEN}{src_table}{_C.RESET}"
        )
        _sep("═")

        # Auto-match Snowflake table with confidence scoring
        sf_table = None
        if sf_tables:
            auto = upper_sf.get(src_table.upper(), "")
            if auto:
                _ok(f"Auto-matched Snowflake table: {auto}  {_C.DIM}(confidence: 100%){_C.RESET}")
                sf_table = auto
            else:
                sm = difflib.SequenceMatcher
                scored = sorted(
                    ((t, int(sm(None, src_table.upper(), t).ratio() * 100)) for t in upper_sf),
                    key=lambda x: x[1], reverse=True,
                )
                close = [(t, pct) for t, pct in scored if pct >= 30][:5]
                if close:
                    print(f"\n  {_C.BOLD}Closest Snowflake tables:{_C.RESET}")
                    for j, (c, pct) in enumerate(close, 1):
                        bar = f"{_C.GREEN}" if pct >= 80 else (f"{_C.YELLOW}" if pct >= 50 else f"{_C.DIM}")
                        print(f"    {_C.CYAN}[{j}]{_C.RESET}  {upper_sf[c]:<40} {bar}{pct}%{_C.RESET}")
                    raw = input(f"  Pick number for '{src_table}' Snowflake table (or type name): ").strip()
                    try:
                        n = int(raw)
                        if 1 <= n <= len(close):
                            sf_table = upper_sf[close[n - 1][0]]
                    except ValueError:
                        sf_table = raw if raw else None

        if not sf_table:
            sf_table = input(
                f"  {_C.BOLD}Snowflake table for '{src_table}'{_C.RESET} "
                f"[{_C.DIM}{src_table.upper()}{_C.RESET}]: "
            ).strip() or src_table.upper()

        print(f"  {_C.DIM}→ {src_table}  ↔  {sf_table}{_C.RESET}")

        try:
            result = pipeline.run(
                pg_schema=pg_schema,
                pg_table=src_table,
                sf_schema=sf_schema,
                sf_table=sf_table,
                sf_database=sf_database,
                pg_database=pg_database,
                exclude_columns=exclude_cols or None,
                output_dir=_output_dir,
            )
            results.append((src_table, sf_table, result))
            succeeded += 1
            _ok(f"Generated: {result.yaml_path.name}")
        except Exception as exc:
            results.append((src_table, sf_table, exc))
            failed += 1
            _err(f"Failed: {exc}")

    # ── Final summary ─────────────────────────────────────────────────────────
    print()
    _sep("═")
    print(f"\n  {_C.BOLD}PARAMETERIZED VALIDATION COMPLETE{_C.RESET}")
    _sep()
    print(f"  Tables processed : {len(source_tables)}")
    print(f"  {_C.GREEN}Succeeded        : {succeeded}{_C.RESET}")
    if failed:
        print(f"  {_C.RED}Failed           : {failed}{_C.RESET}")
    print()

    for src_table, sf_table_used, r in results:
        if isinstance(r, Exception):
            print(f"  {_C.RED}✗{_C.RESET}  {src_table:<30}  error: {r}")
        else:
            print(f"  {_C.GREEN}✓{_C.RESET}  {src_table:<30}  YAML: {r.yaml_path.name}")

    print()
    _sep("═")


def _run_batch_interactive(args) -> None:
    """Guide the user through batch validation interactively."""
    _head("📦  BATCH VALIDATION  (Multiple Tables)")
    print()
    print(f"  Batch mode reads a YAML config and validates all listed tables in parallel.")
    print(f"  {_C.DIM}See docs-v3/BATCH_USAGE.md for the tables.yaml format.{_C.RESET}")

    example_path = _SRC_DIR.parent / "docs-v3" / "examples" / "tables.yaml"
    config_path = _prompt(
        "\nPath to your batch YAML config file",
        str(example_path) if example_path.exists() else "tables.yaml",
    )
    if not os.path.exists(config_path):
        _warn(f"Config file not found: {config_path}")
        _dim("Create a tables.yaml or copy from: docs-v3/examples/tables.yaml")
        return

    dry     = input("\n  Run as dry-run first? [Y/n]: ").strip().lower() not in ("n", "no")
    verbose = input("  Enable verbose output? [y/N]: ").strip().lower() in ("y", "yes")

    args.config  = config_path
    args.dry_run = dry
    args.verbose = verbose
    args.workers = None
    args.model   = None
    cmd_batch(args)


def _run_parameterized_interactive(args) -> None:
    """
    Interactive entry point for the parameterized multi-table flow.
    Prompts the user for table names and optional column exclusions,
    then delegates to _run_parameterized_tables().
    """
    _head("📋  PARAMETERIZED TABLE VALIDATION")
    print()
    print(f"  {_C.DIM}Type comma-separated table names — the pipeline runs once per table.{_C.RESET}")
    print(f"  {_C.DIM}Source connection is selected next. Snowflake table is auto-matched.{_C.RESET}")
    print()

    tables_raw = _prompt("Source table names (comma-separated)  e.g. events,users,orders", "")
    if not tables_raw.strip():
        _warn("No tables entered. Cancelled.")
        return

    exclude_raw = _prompt(
        "Columns to exclude (comma-separated, or press Enter for none)", ""
    )
    exclude_cols = [c.strip() for c in exclude_raw.split(",") if c.strip()]

    current_model = os.getenv("DIAL_MODEL", "gpt-4o")

    args.tables          = tables_raw
    args.source_index    = None
    args.source_schema   = None
    args.source_database = None
    args.sf_schema       = None
    args.sf_database     = None
    args.model           = None

    _run_parameterized_tables(args, current_model, exclude_cols)


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-DB MULTI-TABLE WORKFLOW  (cmd_multi_db + helpers)
# ─────────────────────────────────────────────────────────────────────────────

def _pick_database_from_source(rec: dict) -> str:
    """
    Live-discover databases on the chosen server and let the user pick one.
    Updates rec["database"] in-place and returns the chosen database name.
    Falls back to manual entry if discovery fails.
    """
    from setup_wizard import (
        _discover_postgres_databases,
        _discover_mssql_databases,
    )

    db_type  = _normalize_db_type(rec["db_type"])
    host     = rec["host"]
    port     = int(rec.get("port") or 5432)
    username = rec.get("username", "")
    password = os.getenv(f"{rec['prefix']}PASSWORD", "")
    auth     = rec.get("auth", "")
    default_db = rec.get("database", "")

    _head(f"SELECT DATABASE  ({rec['db_label']}  {host}:{port})")
    _dim("Connecting to list available databases …")

    databases = []
    try:
        if db_type == "postgresql":
            databases = _discover_postgres_databases(host, port, username, password)
        elif db_type == "mssql":
            databases = _discover_mssql_databases(host, port, username, password, auth)
    except Exception as exc:
        _warn(f"Could not list databases: {exc}")

    if databases:
        _ok(f"Found {len(databases)} database(s)")
        print()
        for i, db in enumerate(databases, 1):
            marker = f"  {_C.DIM}← configured{_C.RESET}" if db == default_db else ""
            print(f"    {_C.CYAN}[{i}]{_C.RESET}  {db}{marker}")
        print()
        raw = input(
            f"  Select database number or type name [{_C.DIM}{default_db}{_C.RESET}]: "
        ).strip()
        if not raw:
            chosen = default_db
        else:
            try:
                n = int(raw)
                chosen = databases[n - 1] if 1 <= n <= len(databases) else raw
            except ValueError:
                chosen = raw
    else:
        _dim("Could not retrieve database list — enter name manually.")
        chosen = _prompt("Database name", default_db).strip() or default_db

    rec["database"] = chosen
    _ok(f"Database: {chosen}")
    return chosen


def _pick_schema_from_source(rec: dict) -> str:
    """
    Live-discover schemas in the chosen database and let the user pick one.
    Uses the filtered discovery functions (no system schemas).
    Returns the selected schema name.
    """
    from setup_wizard import (
        _discover_postgres_schemas,
        _discover_mssql_schemas,
    )

    db_type  = _normalize_db_type(rec["db_type"])
    host     = rec["host"]
    port     = int(rec.get("port") or 5432)
    database = rec["database"]
    username = rec.get("username", "")
    password = os.getenv(f"{rec['prefix']}PASSWORD", "")
    auth     = rec.get("auth", "")
    default_schema = rec.get("schema", "public")

    _head(f"SELECT SCHEMA  ({database}  on  {rec['db_label']})")
    _dim(f"Fetching schemas from {database} …")

    schemas = []
    try:
        if db_type == "postgresql":
            schemas = _discover_postgres_schemas(host, port, database, username, password)
        elif db_type == "mssql":
            schemas = _discover_mssql_schemas(host, port, database, username, password, auth)
        else:
            # Generic fallback via extractor
            from sql_extractor import ExtractorFactory
            from setup_wizard import DB_TYPES
            extractor = ExtractorFactory.create(
                db_type,
                host=host, port=port, database=database,
                username=username, password=password, auth=auth,
            )
            schemas = extractor.list_schemas()
    except Exception as exc:
        _warn(f"Could not list schemas: {exc}")
        _dim("You can still type the schema name manually.")

    if schemas:
        _ok(f"Found {len(schemas)} schema(s)")
        print()
        for i, s in enumerate(schemas, 1):
            marker = f"  {_C.DIM}← configured{_C.RESET}" if s == default_schema else ""
            print(f"    {_C.CYAN}[{i}]{_C.RESET}  {s}{marker}")
        print()
        raw = input(
            f"  Select schema number or type name [{_C.DIM}{default_schema}{_C.RESET}]: "
        ).strip()
        if not raw:
            return default_schema
        try:
            n = int(raw)
            if 1 <= n <= len(schemas):
                return schemas[n - 1]
        except ValueError:
            pass
        return raw
    else:
        return _prompt("Schema name", default_schema).strip() or default_schema


def _pick_tables_from_source(rec: dict, schema: str) -> list:
    """
    List tables in the chosen source connection + schema and let the user
    pick multiple (comma-separated numbers and/or names).
    Returns List[str] of selected table names.
    """
    from sql_extractor import ExtractorFactory
    from setup_wizard import DB_TYPES

    _head(f"TABLES IN  {rec['database']}.{schema}  ({rec['db_label']})")
    print(f"  {_C.DIM}Fetching table list ...{_C.RESET}")

    tables = []
    db_type = _normalize_db_type(rec["db_type"])
    try:
        extractor = ExtractorFactory.create(
            db_type,
            host=rec["host"],
            port=int(rec.get("port") or DB_TYPES.get(db_type, ("", 5432))[1]),
            database=rec["database"],
            username=rec["username"],
            password=os.getenv(f"{rec['prefix']}PASSWORD", ""),
            auth=rec.get("auth", ""),
        )
        tables = extractor.list_tables(schema)
    except Exception as exc:
        _warn(f"Could not list tables: {exc}")
        _dim("You can still type table names manually below.")

    if tables:
        col_w = 32
        cols  = max(1, 70 // col_w)
        print()
        for i in range(0, len(tables), cols):
            row_items = tables[i:i + cols]
            parts = []
            for j, t in enumerate(row_items):
                idx = i + j + 1
                parts.append(f"{_C.DIM}{idx:>3}.{_C.RESET} {_C.GREEN}{t:<{col_w}}{_C.RESET}")
            print("    " + "  ".join(parts))
        _dim(f"\n  {len(tables)} table(s) in {rec['database']}.{schema}")
        print()
        print(f"  {_C.DIM}Enter comma-separated numbers, names, or a mix.{_C.RESET}")
        print(f"  {_C.DIM}Example:  1,3,5   or   events,users   or   1,orders,3{_C.RESET}")
        print()

    raw = input("  Tables (comma-separated): ").strip()
    if not raw:
        return []

    selected = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            n = int(token)
            if 1 <= n <= len(tables):
                selected.append(tables[n - 1])
            else:
                _warn(f"  Index {n} out of range — skipped")
        except ValueError:
            selected.append(token)  # raw name

    return selected


def cmd_multi_db(args):
    """
    Run Tables Validation Workflow  (menu [2])
    ──────────────────────────────────────────
    A  Pick source DB (PostgreSQL / MS SQL Server / Athena)
    B  Pick schema from live list
    C  Type comma-separated table names
    D  Optional: exclude columns
    E  Snowflake auto-match for each table
    F  Confirm and run the pipeline
    G  Show final summary

    Outputs per table:
      config/bronze/count_validation/bronze_count_validation.yaml  ← row count (shared)
      config/bronze/data_validation/<table>_validation.yaml        ← column-level validation
      config/bronze/data_validation/<table>_dynamic_suite.yaml     ← schema-aware checks
    """
    from validation_pipeline import ValidationPipeline
    from sql_extractor import SnowflakeExtractor
    import difflib

    _banner()
    _head("📋  RUN TABLES VALIDATION")
    print(f"""
  {_C.DIM}Workflow: pick DB → pick schema → type table names → run pipeline for each.
  Outputs per table:
    • config/bronze/count_validation/bronze_count_validation.yaml  — row count (shared)
    • config/bronze/data_validation/<table>_validation.yaml        — column-level validation{_C.RESET}
""")

    # ── A: Pick source connection ─────────────────────────────────────────────
    source_index  = getattr(args, "source_index", None)
    profile_name  = getattr(args, "connection_profile", None)

    if profile_name:
        _warn("Connection profiles are disabled. Use credentials from .env.")
        rec = _get_connection_by_index(source_index) if source_index is not None else _pick_source_connection()
    elif source_index is not None:
        rec = _get_connection_by_index(source_index)
    else:
        rec = _pick_source_connection()

    if rec is None:
        return

    _override_source_env(rec)

    # ── B: Pick database then schema ──────────────────────────────────────────
    pg_database_arg = getattr(args, "source_database", None) or ""
    if not pg_database_arg:
        _pick_database_from_source(rec)   # updates rec["database"] in-place
    schema = getattr(args, "source_schema", None) or _pick_schema_from_source(rec)
    if not schema:
        _err("No schema selected.")
        return

    # ── C: Table names (comma-separated input) ────────────────────────────────
    tables_arg = getattr(args, "tables", None) or ""
    if tables_arg:
        source_tables = [t.strip() for t in tables_arg.split(",") if t.strip()]
    else:
        _head(f"TABLE NAMES  ({rec['database']}.{schema})")
        print(f"  {_C.DIM}Enter comma-separated table names to validate.{_C.RESET}")
        print(f"  {_C.DIM}Example: general_ledger_line_items,invoices,payments{_C.RESET}")
        print()
        raw = input("  Table names: ").strip()
        source_tables = [t.strip() for t in raw.split(",") if t.strip()]

    if not source_tables:
        _err("No tables entered.")
        return

    pg_database = rec["database"]

    # ── D: Select columns to exclude per source table ─────────────────────────
    # Uses the <db_type>_exclusions.yaml matching this source connection, so
    # entries there apply to BOTH this source type and the Snowflake target.
    src_db_type = _normalize_db_type(rec.get("db_type", "postgresql"))
    exclude_raw = getattr(args, "exclude", None) or ""
    base_exclusions = _get_all_exclusions(src_db_type)
    base_exclusions.extend(c.strip() for c in exclude_raw.split(",") if c.strip())
    _seen_base = set()
    _deduped_base = []
    for c in base_exclusions:
        cl = c.lower()
        if cl not in _seen_base:
            _seen_base.add(cl)
            _deduped_base.append(c)
    base_exclusions = _deduped_base

    want_custom_excl = ""
    if not exclude_raw:
        want_custom_excl = input(
            "\n  Do you want to exclude any custom columns? [y/N]: "
        ).strip().lower()

    if want_custom_excl in ("y", "yes"):
        table_exclusions = {
            table: _select_column_exclusions(rec, schema, table, base_exclusions)
            for table in source_tables
        }
    else:
        table_exclusions = {table: base_exclusions for table in source_tables}

    # ── E: Pick Snowflake target database + schema ────────────────────────────
    sf_database = getattr(args, "sf_database", None) or ""
    sf_schema   = getattr(args, "sf_schema",   None) or ""
    if not sf_database or not sf_schema:
        sf_database, sf_schema = _pick_snowflake_target()

    # ── F: Snowflake table auto-match ─────────────────────────────────────────
    _head("SNOWFLAKE TABLE MATCHING")
    print(f"  {_C.DIM}Target: {sf_database}.{sf_schema}{_C.RESET}")
    print(f"  {_C.DIM}Fetching Snowflake table list for auto-match ...{_C.RESET}")

    sf_tables = []
    try:
        sf_tables = SnowflakeExtractor(database=sf_database).list_tables(sf_schema)
        _ok(f"Found {len(sf_tables)} Snowflake tables in {sf_database}.{sf_schema}")
    except Exception as exc:
        _warn(f"Could not list Snowflake tables: {exc}")
        _dim("You will be asked for each Snowflake table name manually.")

    upper_sf = {t.upper(): t for t in sf_tables}

    table_pairs = []  # [(src_table, sf_table)]
    for src_table in source_tables:
        sf_table = None
        if sf_tables:
            auto = upper_sf.get(src_table.upper(), "")
            if auto:
                sf_table = auto
                _ok(f"Auto-matched  {src_table}  →  {sf_table}  {_C.DIM}(confidence: 100%){_C.RESET}")
            else:
                sm = difflib.SequenceMatcher
                scored = sorted(
                    ((t, int(sm(None, src_table.upper(), t).ratio() * 100)) for t in upper_sf),
                    key=lambda x: x[1], reverse=True,
                )
                close = [(t, pct) for t, pct in scored if pct >= 30][:5]
                if close:
                    print(f"\n  {_C.BOLD}Closest Snowflake tables for '{src_table}':{_C.RESET}")
                    for j, (c, pct) in enumerate(close, 1):
                        bar = f"{_C.GREEN}" if pct >= 80 else (f"{_C.YELLOW}" if pct >= 50 else f"{_C.DIM}")
                        print(f"    {_C.CYAN}[{j}]{_C.RESET}  {upper_sf[c]:<40} {bar}{pct}%{_C.RESET}")
                    raw = input(
                        f"  Pick number for '{src_table}' Snowflake table "
                        f"(or type name) [{_C.DIM}{src_table.upper()}{_C.RESET}]: "
                    ).strip()
                    if not raw:
                        sf_table = src_table.upper()
                    else:
                        try:
                            n = int(raw)
                            sf_table = upper_sf[close[n - 1][0]] if 1 <= n <= len(close) else raw
                        except ValueError:
                            sf_table = raw

        if not sf_table:
            sf_table = input(
                f"  {_C.BOLD}Snowflake table for '{src_table}'{_C.RESET} "
                f"[{_C.DIM}{src_table.upper()}{_C.RESET}]: "
            ).strip() or src_table.upper()

        table_pairs.append((src_table, sf_table))

    # ── F: Confirm ────────────────────────────────────────────────────────────
    current_model = getattr(args, "model", None) or os.getenv("DIAL_MODEL", "gpt-4o")

    print()
    _sep("═")
    print(f"\n  {_C.BOLD}Validation Plan:{_C.RESET}")
    ai_status = (
        f"{_C.GREEN}✓ ACTIVE{_C.RESET}"
        if os.getenv("DIAL_API_KEY")
        else f"{_C.YELLOW}⚠ Not active — static fallback{_C.RESET}"
    )
    print(f"    Source DB   : {_C.GREEN}{rec['db_label']}  {pg_database}.{schema}{_C.RESET}")
    print(f"    Target      : {_C.CYAN}Snowflake  {sf_database}.{sf_schema}{_C.RESET}")
    print(f"    AI Mode     : {ai_status}")
    print(f"    Model       : {_C.CYAN}{current_model}{_C.RESET}")
    if any(table_exclusions.values()):
        print(f"    Exclusions  : {_C.YELLOW}selected per table{_C.RESET}")
    print(f"\n    {_C.BOLD}Tables ({len(table_pairs)}):{_C.RESET}")
    for src, sf in table_pairs:
        print(f"      {_C.GREEN}{src:<30}{_C.RESET}  →  {_C.CYAN}{sf}{_C.RESET}")
    print()

    confirm = input("  Proceed with query generation? [Y/n]: ").strip().lower()
    if confirm in ("n", "no"):
        _dim("Cancelled.")
        return

    # ── Layer selection ───────────────────────────────────────────────────────
    print(f"\n  {_C.BOLD}Select data layer:{_C.RESET}")
    print(f"    {_C.CYAN}[1]{_C.RESET}  bronze  (default)")
    print(f"    {_C.CYAN}[2]{_C.RESET}  silver")
    print(f"    {_C.CYAN}[3]{_C.RESET}  gold")
    _layer_choice = input("  Layer [1]: ").strip() or "1"
    _layer = {"1": "bronze", "2": "silver", "3": "gold"}.get(_layer_choice, "bronze")
    _output_dir = Path(__file__).parent.parent / "Project" / "config" / _layer

    # ── G: Pipeline loop ──────────────────────────────────────────────────────
    src_extractor = _make_source_extractor(rec)
    pipeline  = ValidationPipeline(model=current_model, source_extractor=src_extractor)
    results   = []
    succeeded = 0
    failed    = 0

    for i, (src_table, sf_table) in enumerate(table_pairs, 1):
        print()
        _sep("═")
        print(
            f"  {_C.BOLD}{_C.MAGENTA}TABLE {i}/{len(table_pairs)}{_C.RESET}  "
            f"{_C.GREEN}{src_table}{_C.RESET}  →  {_C.CYAN}{sf_table}{_C.RESET}"
        )
        _sep("═")

        try:
            result = pipeline.run(
                pg_schema=schema,
                pg_table=src_table,
                sf_schema=sf_schema,
                sf_table=sf_table,
                sf_database=sf_database,
                pg_database=pg_database,
                exclude_columns=table_exclusions.get(src_table) or None,
                output_dir=_output_dir,
            )
            results.append((src_table, sf_table, result))
            succeeded += 1
            if getattr(result, "count_yaml_path", None):
                _ok(f"Count YAML  : {result.count_yaml_path.name}")
            _ok(f"Full YAML   : {result.yaml_path.name}")
        except Exception as exc:
            results.append((src_table, sf_table, exc))
            failed += 1
            _err(f"Failed: {exc}")

    # ── H: Final summary ──────────────────────────────────────────────────────
    print()
    _sep("═")
    print(f"\n  {_C.BOLD}MULTI-TABLE VALIDATION COMPLETE{_C.RESET}")
    _sep()
    print(f"  Tables processed : {len(table_pairs)}")
    print(f"  {_C.GREEN}Succeeded        : {succeeded}{_C.RESET}")
    if failed:
        print(f"  {_C.RED}Failed           : {failed}{_C.RESET}")
    print()

    # Summary table header
    print(f"  {'Source Table':<28} {'Snowflake Table':<28} {'Count YAML':<28} Status")
    _sep("─", 100)
    for src_table, sf_table, r in results:
        if isinstance(r, Exception):
            print(
                f"  {_C.RED}✗{_C.RESET}  {src_table:<26} {sf_table:<28} {'—':<28} error: {r}"
            )
        else:
            count_name = r.count_yaml_path.name if getattr(r, "count_yaml_path", None) else "—"
            print(
                f"  {_C.GREEN}✓{_C.RESET}  {src_table:<26} {sf_table:<28} {count_name:<28} "
                f"{r.yaml_path.name}"
            )
    print()
    _sep("═")

    # Offer to save connection as a reusable profile
    # if not profile_name and rec is not None:
    #     _save_session_as_profile(rec, sf_database, sf_schema)


def _blank():
    print()


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND: profiles  — manage saved connection profiles
# ─────────────────────────────────────────────────────────────────────────────

def cmd_profiles(args):
    """
    Manage named connection profiles stored in ~/.migration-validator/profiles.json.

    Sub-actions:
      list    — show all saved profiles  (default)
      delete  — delete a profile by name
    """
    action = getattr(args, "profile_action", "list") or "list"

    _banner()
    _head("💾  CONNECTION PROFILES")
    print(f"  {_C.DIM}Profiles are stored in: {_PROFILES_PATH}{_C.RESET}\n")

    profiles = _profile_mgr.list_profiles()

    if action == "delete":
        name = getattr(args, "profile_name", None)
        if not name:
            if not profiles:
                _warn("No profiles saved yet.")
                return
            print(f"  {_C.BOLD}Select profile to delete:{_C.RESET}\n")
            for i, p in enumerate(profiles, 1):
                print(f"    {_C.CYAN}[{i}]{_C.RESET}  {p}")
            print()
            raw = input("  Enter number or name: ").strip()
            try:
                n = int(raw)
                if 1 <= n <= len(profiles):
                    name = profiles[n - 1]
            except ValueError:
                name = raw
        if name:
            confirm = input(f"  Delete profile '{name}'? [y/N]: ").strip().lower()
            if confirm in ("y", "yes"):
                if _profile_mgr.delete_profile(name):
                    _ok(f"Profile '{name}' deleted.")
                else:
                    _err(f"Profile '{name}' not found.")
        return

    # ── list (default) ────────────────────────────────────────────────────────
    if not profiles:
        _warn("No profiles saved yet.")
        print(f"\n  {_C.DIM}Profiles are created automatically after a generate or multi run.")
        print(f"  Or use  --connection-profile <name>  to use an existing one.{_C.RESET}")
        return

    print(f"  {'Profile':<20}  {'Type':<14}  {'Source':<40}  {'Snowflake Target'}")
    _sep("─", 100)
    for name in profiles:
        p = _profile_mgr.get_profile(name)
        if not p:
            continue
        src = p.get("source", {})
        sf  = p.get("snowflake", {})
        db_type  = _normalize_db_type(src.get("db_type", "postgresql"))
        db_label = _DB_TYPE_LABELS.get(db_type, db_type)
        src_str  = f"{src.get('host','')}:{src.get('port','')}/{src.get('database','')}.{src.get('schema','')}"
        sf_str   = f"{sf.get('database','')}.{sf.get('schema','')}"
        created  = p.get("created_at", "")[:10]
        print(
            f"  {_C.CYAN}{name:<20}{_C.RESET}"
            f"  {db_label:<14}"
            f"  {_C.GREEN}{src_str:<40}{_C.RESET}"
            f"  {_C.DIM}{sf_str}  (saved {created}){_C.RESET}"
        )
    print()
    _dim("To use a profile:  python validate_cli.py generate --connection-profile <name>")
    _dim("To delete:         python validate_cli.py profiles delete [name]")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# lint — validate generated configs before anything touches a database
# ─────────────────────────────────────────────────────────────────────────────

def cmd_lint(args):
    """
    Check every generated validation YAML for structural problems.

    Catches, at zero cost and with no database connection:
      - duplicate table keys (YAML keeps the last one silently)
      - missing or misspelled required fields
      - queries that are not SELECT statements
      - source_name / target_name that resolve to no .env credentials
      - YAML that has drifted from the canonical plan that produced it

    Exit code 0 = clean, 1 = problems found. Safe to run in CI.
    """
    from validation.config_schema import validate_config_dir
    from core.plan_store import PlanStore, PlanStoreError

    _banner()
    _head("CONFIG LINT")

    config_dir = Path(args.config_dir) if args.config_dir else (_SRC_DIR.parent / "config")
    if not config_dir.exists():
        _err(f"Config directory not found: {config_dir}")
        sys.exit(1)

    _dim(f"Scanning {config_dir}")
    _sep()

    results = validate_config_dir(config_dir, check_credentials=not args.skip_credentials)

    if not results:
        _warn("No YAML config files found. Generate some first:")
        _dim("python src/validate_cli.py generate --source-table <t> --sf-table <T>")
        sys.exit(0)

    total_errors = 0
    for path, errors in results.items():
        rel = path.relative_to(config_dir.parent)
        if errors:
            total_errors += len(errors)
            _err(f"{rel}  ({len(errors)} problem(s))")
            for message in errors:
                _dim(f"  → {message}")
        else:
            _ok(f"{rel}")

    # ── Drift check: does the YAML still agree with its plan? ────────────────
    _sep()
    _head("PLAN ↔ CONFIG CONSISTENCY")
    store = PlanStore()
    plans = store.list_plans()
    if not plans:
        _warn("No canonical plans found in output/plans/.")
        _dim("Column coverage cannot be verified — regenerate to record exclusions.")
    else:
        for plan_path in plans:
            try:
                plan = store.load(plan_path)
            except PlanStoreError as exc:
                total_errors += 1
                _err(f"{plan_path.name}: {exc}")
                continue

            _layers = ("bronze", "silver", "gold")
            expected = next(
                (config_dir / ly / "data_validation" / f"{plan.source_table.lower()}.yaml"
                 for ly in _layers
                 if (config_dir / ly / "data_validation" / f"{plan.source_table.lower()}.yaml").exists()),
                None,
            )
            if expected is None:
                total_errors += 1
                _err(f"{plan.source_table}: plan exists but no matching YAML found in bronze/silver/gold — regenerate.")
                continue

            coverage = plan.exclusion_summary()
            marker = "✓" if coverage["coverage_pct"] >= 80 else "!"
            _dim(
                f"  {marker} {plan.source_table}: "
                f"{coverage['validated']}/{coverage['total_source_columns']} columns "
                f"({coverage['coverage_pct']}%)"
            )

    _sep()
    if total_errors:
        _err(f"LINT FAILED — {total_errors} problem(s) found.")
        _dim("Configs are generated artefacts: fix by regenerating, not by hand-editing.")
        sys.exit(1)

    _ok("LINT PASSED — all configs are structurally valid.")
    sys.exit(0)


# ─────────────────────────────────────────────────────────────────────────────
# CLI argument parser + entry point
# ─────────────────────────────────────────────────────────────────────────────

def cmd_batch(args):
    """
    Batch validation workflow:
      Read a YAML config → process N tables → write _manifest.json

    Usage:
      python validate_cli.py batch --config tables.yaml
      python validate_cli.py batch --config tables.yaml --dry-run
      python validate_cli.py batch --config tables.yaml --verbose --workers 8
    """
    from batch import load_batch_config, BatchRunner

    config_path = getattr(args, "config", None)
    if not config_path:
        _err("--config is required for batch mode.")
        sys.exit(1)

    _banner()
    _head("📦  BATCH VALIDATION")

    try:
        config = load_batch_config(config_path)
    except Exception as exc:
        _err(f"Failed to parse batch config: {exc}")
        sys.exit(1)

    _ok(f"Config loaded: {config_path}")
    _dim(f"  Source  : {config.source.db_type} @ {config.source.host}/{config.source.database}.{config.source.schema}")
    _dim(f"  Target  : snowflake @ {config.target.database}.{config.target.schema}")
    _dim(f"  Tables  : {len(config.tables)}")
    _dim(f"  Parallel: {config.execution.parallel}  max_workers={config.execution.max_workers}")
    _dim(f"  fail_fast: {config.execution.fail_fast}")

    if getattr(args, "workers", None):
        config.execution.max_workers = args.workers

    dry_run = getattr(args, "dry_run", False)
    verbose = getattr(args, "verbose", False)
    model   = getattr(args, "model", None)

    if dry_run:
        _warn("DRY RUN mode — no files will be written.")

    runner = BatchRunner(
        dry_run=dry_run,
        verbose=verbose,
        model=model,
    )

    try:
        manifest_path = runner.run(config)
        if not dry_run:
            _ok(f"Batch complete. Manifest: {manifest_path}")
    except Exception as exc:
        _err(f"Batch run failed: {exc}")
        if verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


def cmd_excel_batch(args):
    """
    Excel-driven batch validation for report packs.

    Reads an XLSX mapping sheet where each row is a report validation spec.
    Generates AI SQL for empty query cells, applies env substitution, and
    writes YAML configs to Project/config/report/<pack>/data_validation/.

    Usage:
      python validate_cli.py excel-batch --file mapping.xlsx
      python validate_cli.py excel-batch --file mapping.xlsx --env dev
      python validate_cli.py excel-batch --file mapping.xlsx --env prod --sheet Sheet2
      python validate_cli.py excel-batch --file mapping.xlsx --dry-run
    """
    from excel_batch_loader import ExcelBatchLoader

    file_path = getattr(args, "file", None)
    if not file_path:
        _err("--file is required for excel-batch mode.")
        sys.exit(1)

    _banner()
    _head("📊  EXCEL BATCH — Report Pack Validation Generator")

    env     = getattr(args, "env", None) or None
    sheet   = getattr(args, "sheet", None) or None
    model   = getattr(args, "model", None) or None
    dry_run = getattr(args, "dry_run", False)

    if env:
        _ok(f"Environment: {env}  ({{env}} tokens will be replaced)")
    else:
        _warn("No --env specified — {env} tokens kept as template placeholders in YAML")

    loader = ExcelBatchLoader(env=env, model=model, dry_run=dry_run)
    try:
        written = loader.run(file_path, sheet=sheet)
        if not dry_run:
            _ok(f"Done. {len(written)} YAML file(s) written.")
        else:
            _warn(f"Dry run complete. {len(written)} file(s) would be written.")
    except Exception as exc:
        _err(f"Excel batch failed: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate_cli",
        description="Migration Validator — AI-Powered SQL + YAML Generator (multi-DB → Snowflake)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
First time? Run the setup wizard:
  python validate_cli.py setup

Single table:
  python validate_cli.py generate --source-database fms --source-table events --sf-table EVENTS
  python validate_cli.py generate --source-table events --sf-table EVENTS --model gpt-4o

Multiple tables:
  python validate_cli.py batch --config tables.yaml
  python validate_cli.py batch --config tables.yaml --dry-run --verbose

Utilities:
  python validate_cli.py lint
  python validate_cli.py rules
  python validate_cli.py add-rule
  python validate_cli.py list-models
  python validate_cli.py list-tables
        """,
    )
    sub = parser.add_subparsers(dest="command")

    # ── setup ────────────────────────────────────────────────────────────────
    sub.add_parser(
        "setup",
        help="First-run wizard: configure 1–10 source databases + Snowflake + AI in .env",
    )

    # ── connections ───────────────────────────────────────────────────────────
    sub.add_parser(
        "connections",
        help="Show all configured source connections with live ping and table counts",
    )

    # ── generate ─────────────────────────────────────────────────────────────
    gen = sub.add_parser(
        "generate",
        help="Generate SQL + YAML validation files (full workflow)",
    )
    gen.add_argument("--source-database", dest="source_database", default=None, help="Source database name — PostgreSQL/MSSQL/Athena (overrides SOURCE_DATABASE in .env)")
    gen.add_argument("--source-schema",   dest="source_schema",   default=None, help="Source schema")
    gen.add_argument("--source-table",    dest="source_table",    default=None, help="Source table (single table mode)")
    gen.add_argument("--sf-schema",   dest="sf_schema",   default=None, help="Snowflake schema")
    gen.add_argument("--sf-table",    dest="sf_table",    default=None, help="Snowflake table")
    gen.add_argument("--sf-database", dest="sf_database", default=None, help="Snowflake database")
    gen.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="AI model to use. Run 'list-models' to see working options for your API key.",
    )
    gen.add_argument(
        "--source", "-s",
        dest="source_index",
        type=int,
        default=None,
        metavar="N",
        help="Pre-select source connection by SRC_N index (e.g. --source 2 uses SRC_2). Skips interactive picker.",
    )
    gen.add_argument(
        "--tables",
        dest="tables",
        default=None,
        metavar="T1,T2,...",
        help="Comma-separated source table names. Runs the pipeline once per table. Requires --source N.",
    )
    gen.add_argument(
        "--exclude",
        dest="exclude",
        default=None,
        metavar="COL1,COL2,...",
        help="Comma-separated column names to exclude from validation (case-insensitive).",
    )
    gen.add_argument(
        "--connection-profile",
        dest="connection_profile",
        default=None,
        metavar="NAME",
        help="Use a saved connection profile (skips all interactive pickers). "
             "Run 'profiles' to see available profiles.",
    )
    gen.add_argument(
        "--layer",
        dest="layer",
        default=None,
        choices=("bronze", "silver", "gold"),
        help="Medallion layer — output folder for the generated config (default: bronze).",
    )

    # ── multi ─────────────────────────────────────────────────────────────────
    mlt = sub.add_parser(
        "multi",
        help="Multi-DB multi-table: pick DB → schema → tables → generate SQL + 2 YAML files per table",
    )
    mlt.add_argument("--source", "-s", dest="source_index", type=int, default=None,
                     metavar="N", help="Pre-select SRC_N connection (skips interactive picker)")
    mlt.add_argument("--schema", dest="source_schema", default=None, metavar="SCHEMA",
                     help="Source schema (skips interactive picker)")
    mlt.add_argument("--tables", dest="tables", default=None, metavar="T1,T2,...",
                     help="Comma-separated table names (skips interactive picker)")
    mlt.add_argument("--sf-schema", dest="sf_schema", default=None,
                     help="Snowflake schema (overrides SNOWFLAKE_SCHEMA in .env)")
    mlt.add_argument("--sf-database", dest="sf_database", default=None,
                     help="Snowflake database (overrides SNOWFLAKE_DATABASE in .env)")
    mlt.add_argument("--exclude", dest="exclude", default=None, metavar="COL1,COL2,...",
                     help="Comma-separated column names to exclude from validation")
    mlt.add_argument("--model", default=None, metavar="MODEL",
                     help="AI model to use (run 'list-models' to see options)")
    mlt.add_argument(
        "--connection-profile",
        dest="connection_profile",
        default=None,
        metavar="NAME",
        help="Use a saved connection profile (skips all interactive pickers). "
             "Run 'profiles' to see available profiles.",
    )

    # ── profiles ──────────────────────────────────────────────────────────────
    prof = sub.add_parser(
        "profiles",
        help="List, use, or delete saved connection profiles (~/.migration-validator/profiles.json)",
    )
    prof_sub = prof.add_subparsers(dest="profile_action")
    prof_sub.add_parser("list",   help="List all saved profiles (default)")
    prof_del = prof_sub.add_parser("delete", help="Delete a saved profile by name")
    prof_del.add_argument("profile_name", nargs="?", default=None,
                           help="Name of the profile to delete (prompted if omitted)")

    # ── batch ─────────────────────────────────────────────────────────────────
    bat = sub.add_parser(
        "batch",
        help="Batch validate multiple tables from a YAML config file",
    )
    bat.add_argument(
        "--config", "-c",
        required=True,
        metavar="FILE",
        help="Path to the batch YAML config file (e.g. tables.yaml)",
    )
    bat.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Show what would be done without executing",
    )
    bat.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable DEBUG-level logging and full tracebacks on failure",
    )
    bat.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help="Override max_workers from config (parallel threads)",
    )
    bat.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="AI model to use for all tables in the batch",
    )

    # ── lint ──────────────────────────────────────────────────────────────────
    lint = sub.add_parser(
        "lint",
        help="Validate generated YAML configs (duplicates, schema, .env refs) — no DB needed",
    )
    lint.add_argument(
        "--config-dir",
        dest="config_dir",
        default=None,
        metavar="DIR",
        help="Config directory to scan (default: ./config)",
    )
    lint.add_argument(
        "--skip-credentials",
        dest="skip_credentials",
        action="store_true",
        default=False,
        help="Skip checking that source_name/target_name resolve in the environment",
    )

    # ── rules ─────────────────────────────────────────────────────────────────
    sub.add_parser("rules", help="Show the full rule book (base + learned)")

    # ── add-rule ──────────────────────────────────────────────────────────────
    sub.add_parser("add-rule", help="Add a custom rule to the evolving rule book")

    # ── list-models ───────────────────────────────────────────────────────────
    sub.add_parser("list-models", help="List all available AI models")

    # ── list-tables ───────────────────────────────────────────────────────────
    lt = sub.add_parser(
        "list-tables",
        help="List tables in all configured source databases and Snowflake",
    )
    lt.add_argument(
        "--filter", "-f",
        dest="filter",
        default=None,
        metavar="PATTERN",
        help="Only show tables whose name contains PATTERN (case-insensitive)",
    )

    # ── excel-batch ───────────────────────────────────────────────────────────
    eb = sub.add_parser(
        "excel-batch",
        help="Generate YAML validation configs from an Excel report-pack mapping sheet",
    )
    eb.add_argument("--file", "-f", dest="file", required=True,
                    help="Path to the Excel (.xlsx) mapping file")
    eb.add_argument("--env", dest="env", default=None,
                    help="Environment name (dev/prod/…) — replaces {env} tokens in SQL")
    eb.add_argument("--sheet", dest="sheet", default=None,
                    help="Sheet name or index (default: first sheet)")
    eb.add_argument("--model", dest="model", default=None,
                    help="AI model for query generation when cells are empty")
    eb.add_argument("--dry-run", dest="dry_run", action="store_true", default=False,
                    help="Print what would be written without creating files")

    return parser


def main():
    parser = _build_parser()
    args   = parser.parse_args()

    commands = {
        "setup":       cmd_setup,
        "connections": cmd_connections,
        "generate":    cmd_generate,
        "multi":       cmd_multi_db,
        "batch":       cmd_batch,
        "lint":        cmd_lint,
        "rules":       cmd_rules,
        "add-rule":    cmd_add_rule,
        "list-models": lambda _: _list_models_cmd(),
        "list-tables":  cmd_list_tables,
        "profiles":     cmd_profiles,
        "excel-batch":  cmd_excel_batch,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        # No command → interactive menu
        cmd_interactive()


if __name__ == "__main__":
    main()
