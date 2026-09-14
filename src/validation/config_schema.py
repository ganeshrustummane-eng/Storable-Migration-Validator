"""
Validation Config Schema
=========================
Pydantic models for the generated validation YAML.

Why this exists
---------------
YAML configs used to be loaded with ``yaml.safe_load`` and consumed directly.
A typo like ``source_query`` instead of ``sourcequery`` therefore surfaced deep
inside execution, after connections were opened, as an unhelpful KeyError. And
a duplicate top-level table key resolved silently by last-wins, so the file
disagreed with itself about what would run.

Validating here moves every structural failure to load time, before a single
database connection is opened, with a message that names the file, the table,
and the offending field.

Scope
-----
This validates STRUCTURE and REFERENCES:
  - required keys are present and non-empty
  - queries look like SELECT statements
  - `source_name` / `target_name` resolve to credentials that exist in .env
  - no duplicate table keys in a document

It does NOT validate SQL semantics — that is the database's job, and the
dialect checks in the AI SQL generator already gate generation.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

SUPPORTED_SOURCES = {"postgresql", "postgres", "mssql", "sqlserver", "athena", "snowflake"}
_SELECT_RE = re.compile(r"^\s*(--[^\n]*\n|\s)*select\b", re.IGNORECASE)


class ConfigValidationError(ValueError):
    """Raised when a validation config file is structurally invalid."""


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

class _QueryBlock(BaseModel):
    """Fields common to count and data validation blocks."""

    model_config = ConfigDict(extra="forbid")

    source_table_name: str = Field(min_length=1)
    source: str = Field(min_length=1)
    sourcequery: str = Field(min_length=1)
    target_table_name: str = Field(min_length=1)
    target: str = Field(min_length=1)
    targetquery: str = Field(min_length=1)

    source_name: Optional[str] = None
    target_name: Optional[str] = None
    comparison_note: Optional[str] = None
    source_database: Optional[str] = None
    source_schema: Optional[str] = None
    target_database: Optional[str] = None
    target_schema: Optional[str] = None
    source_audit_column: Optional[str] = None
    target_audit_column: Optional[str] = None

    @field_validator("source", "target")
    @classmethod
    def _known_dialect(cls, value: str) -> str:
        if value.strip().lower() not in SUPPORTED_SOURCES:
            raise ValueError(
                f"unsupported database type '{value}'. "
                f"Expected one of: {', '.join(sorted(SUPPORTED_SOURCES))}"
            )
        return value

    @field_validator("sourcequery", "targetquery")
    @classmethod
    def _looks_like_select(cls, value: str) -> str:
        if not _SELECT_RE.match(value):
            raise ValueError(
                "query must be a SELECT statement (found: "
                f"{value.strip().splitlines()[0][:60] if value.strip() else 'empty'!r})"
            )
        return value


class CountValidationBlock(_QueryBlock):
    """A ``count_validation`` block: one COUNT(*) per side."""


class DataValidationBlock(_QueryBlock):
    """A ``data_validation`` block: normalised row-by-row comparison."""

    pksourcecolumn: Optional[Any] = None   # str (single PK) or List[str] (composite)
    pktargetcolumn: Optional[Any] = None   # str (single PK) or List[str] (composite)
    # Custom SQL-only fields
    report_tile: Optional[str] = None
    test_case: Optional[str] = None
    summary: Optional[str] = None
    sourcecolumn: Optional[str] = None
    targetcolumn: Optional[str] = None


class TableValidations(BaseModel):
    model_config = ConfigDict(extra="allow")

    count_validation: Optional[CountValidationBlock] = None
    data_validation: Optional[DataValidationBlock] = None


class TableEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validations: TableValidations


class ValidationConfigDocument(BaseModel):
    """Top-level shape of a generated validation YAML file."""

    model_config = ConfigDict(extra="forbid")

    tables: Dict[str, TableEntry]

    @field_validator("tables")
    @classmethod
    def _non_empty(cls, value: Dict[str, TableEntry]) -> Dict[str, TableEntry]:
        if not value:
            raise ValueError("'tables' must contain at least one table")
        return value


# ---------------------------------------------------------------------------
# File-level checks that pydantic cannot see
# ---------------------------------------------------------------------------

def find_duplicate_table_keys(path: Path) -> List[str]:
    """
    Detect duplicate top-level table keys by scanning raw text.

    yaml.safe_load silently collapses duplicates (last wins), so by the time a
    document reaches pydantic the evidence is gone. This is exactly the bug
    that let bronze.yaml carry two conflicting AcctSoftware blocks.
    """
    seen: Dict[str, int] = {}
    duplicates: List[str] = []
    in_tables = False

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw_line.startswith(" ") and stripped.rstrip(":") == "tables":
            in_tables = True
            continue
        if in_tables and not raw_line.startswith(" "):
            in_tables = False
        if not in_tables:
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent == 2 and stripped.endswith(":"):
            key = stripped[:-1].strip()
            seen[key] = seen.get(key, 0) + 1
            if seen[key] == 2:
                duplicates.append(key)
    return duplicates


def missing_credentials(document: ValidationConfigDocument) -> List[str]:
    """
    Report ``source_name`` / ``target_name`` values with no matching .env entry.

    Credentials are referenced indirectly (``source_name: SRC_1``), so a rename
    in .env would otherwise fail mid-run, after other tables had already been
    validated against live databases.
    """
    problems: List[str] = []
    for table_name, entry in document.tables.items():
        for kind in ("count_validation", "data_validation"):
            block = getattr(entry.validations, kind, None)
            if block is None:
                continue
            for field_name in ("source_name", "target_name"):
                ref = getattr(block, field_name, None)
                if not ref:
                    continue
                if not _credential_exists(ref):
                    problems.append(
                        f"{table_name}.{kind}.{field_name} = '{ref}' "
                        f"but no '{ref}_*' variables are defined in the environment"
                    )
    return problems


def _credential_exists(prefix: str) -> bool:
    marker = f"{prefix.rstrip('_')}_"
    return any(key.startswith(marker) for key in os.environ)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def validate_config_file(
    path: Path, check_credentials: bool = True
) -> Tuple[Optional[ValidationConfigDocument], List[str]]:
    """
    Validate one YAML config file.

    Args:
        path              : Path to the YAML file
        check_credentials : Also verify source_name/target_name resolve in .env

    Returns:
        (document, errors). ``document`` is None when the file could not be
        parsed or failed schema validation. ``errors`` is empty on success.
    """
    errors: List[str] = []

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, [f"{path.name}: not valid YAML — {exc}"]
    except OSError as exc:
        return None, [f"{path.name}: cannot be read — {exc}"]

    if raw is None:
        return None, [f"{path.name}: file is empty"]

    for key in find_duplicate_table_keys(path):
        errors.append(
            f"{path.name}: table '{key}' is defined more than once. "
            "YAML keeps only the last definition, so the file contradicts itself. "
            "Regenerate it — generation upserts and never appends."
        )

    try:
        document = ValidationConfigDocument.model_validate(raw)
    except ValidationError as exc:
        for issue in exc.errors():
            location = ".".join(str(part) for part in issue["loc"])
            errors.append(f"{path.name}: {location} — {issue['msg']}")
        return None, errors

    if check_credentials:
        errors.extend(f"{path.name}: {problem}" for problem in missing_credentials(document))

    return document, errors


def is_validation_config(path: Path) -> bool:
    """
    True when a YAML file is a generated validation config.

    config/ also holds hand-authored policy files (exclusions.yaml,
    database_registry.yaml) with entirely different shapes. Validating those
    against the validation schema would produce noise that trains people to
    ignore lint output.
    """
    parents = {p.name for p in path.parents}
    return "count_validation" in parents or "data_validation" in parents


def validate_config_dir(
    config_dir: Path, check_credentials: bool = True
) -> Dict[Path, List[str]]:
    """
    Validate every generated validation YAML under a config directory.

    Hand-authored policy files are skipped — see is_validation_config().

    Returns:
        Mapping of file path → list of errors. Files with no errors map to [].
    """
    results: Dict[Path, List[str]] = {}
    for path in sorted(config_dir.rglob("*.yaml")):
        if not is_validation_config(path):
            continue
        _, errors = validate_config_file(path, check_credentials=check_credentials)
        results[path] = errors
    return results
