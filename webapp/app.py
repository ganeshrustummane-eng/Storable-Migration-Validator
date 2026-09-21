"""
Migration Validator — Web UI
==============================
A thin Streamlit UI over the existing validate_cli.py logic. Goal: replace
the multi-step interactive terminal flow (pick source -> pick database ->
pick schema -> pick table -> pick Snowflake table -> exclude y/n -> model
y/n -> confirm -> layer choice) with one page per workflow, using live
dropdowns (discovered from the actual database using .env credentials)
instead of sequential prompts.

This file does NOT reimplement any connection/matching/generation logic —
it imports and calls the same functions validate_cli.py and setup_wizard.py
use, so behavior (and correctness fixes made there) stays identical in both
places.

Run with:
    streamlit run webapp/app.py
"""

import difflib
import os
import re
import sys
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="snowflake.connector")
from pathlib import Path

_WEBAPP_DIR = Path(__file__).parent
_ROOT_DIR   = _WEBAPP_DIR.parent
_SRC_DIR    = _ROOT_DIR / "src"
_PROJECT_DIR = _ROOT_DIR / "Project"

for p in (str(_SRC_DIR), str(_ROOT_DIR), str(_PROJECT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import streamlit as st

from dotenv import load_dotenv
load_dotenv(_ROOT_DIR / ".env")

from setup_wizard import (
    print_connection_registry,
    _discover_postgres_databases, _discover_postgres_schemas,
    _discover_mssql_databases, _discover_mssql_schemas,
    _discover_snowflake_databases, _discover_snowflake_schemas,
)
from validate_cli import (
    _normalize_db_type, _DB_TYPE_LABELS, _apply_database_registry,
    _override_source_env, _make_source_extractor, _get_all_exclusions,
    _save_global_user_exclusion, _remove_global_user_exclusion, _exclusions_path_for, STATIC_EXCLUDE_COLUMNS,
)
from validation_pipeline import ValidationPipeline
from sql_extractor import ExtractorFactory, SnowflakeExtractor
from rule_book import rule_book, RuleEntry, RuleValidationError
from ai_transformation.ai_rule_mapper import AVAILABLE_MODELS, MODEL_DESCRIPTIONS, AIRuleMappingError
from ai_transformation.rule_prompt_parser import RuleTypeParser, RuleParseError
from model_probe import get_working_models
from learning.feedback import FeedbackRecorder, MismatchFeedback
import mapping_store
from generated_queries.ai_sql_generator import AISQLQueryGenerator, AISQLGenerationError
from runner import list_configured_tables, run_validation
import results_store

sys.path.insert(0, str(_ROOT_DIR / "token_usage_analysis"))
from report_token_usage import _load_records as _load_token_records, _load_pricing, _cost_for

st.set_page_config(
    page_title="Migration Validator · Enterprise",
    page_icon="🔷",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL ENTERPRISE THEME
# A single, coherent CSS block that covers: tab bar, metrics, buttons, forms,
# expanders, code blocks, badges, and the chat widget.
# Palette: indigo-600 (#4F46E5) primary, slate-900 (#0F172A) heading text,
#          emerald-600 (#059669) success, rose-600 (#E11D48) danger,
#          amber-500 (#F59E0B) warning — all WCAG AA against white.
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* ── Google Fonts ─────────────────────────────────────────────── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

/* ── Root variables ───────────────────────────────────────────── */
:root {
    --primary:        #4F46E5;
    --primary-light:  #818CF8;
    --primary-xlight: #EEF2FF;
    --success:        #059669;
    --success-bg:     #ECFDF5;
    --danger:         #E11D48;
    --danger-bg:      #FFF1F2;
    --warning:        #D97706;
    --warning-bg:     #FFFBEB;
    --neutral-50:     #F8FAFC;
    --neutral-100:    #F1F5F9;
    --neutral-200:    #E2E8F0;
    --neutral-700:    #334155;
    --neutral-900:    #0F172A;
    --radius-sm:      6px;
    --radius-md:      10px;
    --radius-lg:      16px;
    --shadow-sm:      0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.06);
    --shadow-md:      0 4px 12px rgba(0,0,0,.12);
}

/* ── Base font ────────────────────────────────────────────────── */
html, body, [class*="css"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
}

/* ── App background ───────────────────────────────────────────── */
[data-testid="stAppViewContainer"] > .main {
    background: #F8FAFC;
}
[data-testid="stSidebar"] {
    background: #1E1B4B !important;
    border-right: 1px solid #312E81;
}
[data-testid="stSidebar"] * { color: #E0E7FF !important; }

/* Sidebar code blocks — keep dark background but use readable light text */
[data-testid="stSidebar"] .stCode,
[data-testid="stSidebar"] pre,
[data-testid="stSidebar"] [data-testid="stCode"],
[data-testid="stSidebar"] [data-testid="stCode"] pre,
[data-testid="stSidebar"] [data-testid="stCode"] code {
    background: #0F172A !important;
    color: #BAC8FF !important;
    border: 1px solid #312E81 !important;
}

/* Main area code blocks — light background, dark text (fixes the original issue) */
.main .stCode,
.main pre,
.main [data-testid="stCode"],
.main [data-testid="stCode"] pre,
.main [data-testid="stCode"] code,
[data-testid="stAppViewContainer"] > .main .stCode,
[data-testid="stAppViewContainer"] > .main pre {
    background: #F1F5F9 !important;
    color: #1E293B !important;
    border: 1px solid #E2E8F0 !important;
}
[data-testid="stSidebar"] .stMarkdown h1,
[data-testid="stSidebar"] .stMarkdown h2,
[data-testid="stSidebar"] .stMarkdown h3 { color: #C7D2FE !important; }
[data-testid="stSidebar"] [data-testid="stExpander"] {
    background: rgba(255,255,255,0.06) !important;
    border: 1px solid rgba(255,255,255,0.12) !important;
    border-radius: var(--radius-md) !important;
}

/* ── Tab bar ──────────────────────────────────────────────────── */
[data-testid="stTabs"] > div:first-child {
    border-bottom: 2px solid var(--neutral-200) !important;
    gap: 0 !important;
}
[data-testid="stTab"] {
    padding: 0.75rem 1.25rem !important;
    border-radius: var(--radius-sm) var(--radius-sm) 0 0 !important;
    border: none !important;
    background: transparent !important;
    transition: background 0.18s ease, color 0.18s ease !important;
}
[data-testid="stTab"] p {
    font-size: 0.875rem !important;
    font-weight: 600 !important;
    color: var(--neutral-700) !important;
    letter-spacing: 0.01em;
}
[data-testid="stTab"]:hover {
    background: var(--primary-xlight) !important;
}
[data-testid="stTab"]:hover p { color: var(--primary) !important; }
[data-testid="stTab"][aria-selected="true"] {
    background: white !important;
    border-bottom: 2px solid var(--primary) !important;
    margin-bottom: -2px !important;
}
[data-testid="stTab"][aria-selected="true"] p { color: var(--primary) !important; }

/* ── Metric cards ─────────────────────────────────────────────── */
div[data-testid="stMetric"] {
    background: white !important;
    border: 1px solid var(--neutral-200) !important;
    border-radius: var(--radius-md) !important;
    padding: 16px 20px !important;
    box-shadow: var(--shadow-sm) !important;
}
div[data-testid="stMetricValue"] {
    color: var(--primary) !important;
    font-size: 1.75rem !important;
    font-weight: 700 !important;
}
div[data-testid="stMetricLabel"] {
    color: var(--neutral-700) !important;
    font-size: 0.8rem !important;
    font-weight: 500 !important;
    text-transform: uppercase;
    letter-spacing: 0.06em;
}
div[data-testid="stMetricDelta"] { font-size: 0.78rem !important; }

/* ── Buttons ──────────────────────────────────────────────────── */
button[data-testid="baseButton-primary"] {
    background: linear-gradient(135deg, var(--primary) 0%, #6366F1 100%) !important;
    color: white !important; border: none !important;
    border-radius: var(--radius-sm) !important;
    font-weight: 600 !important; font-size: 0.875rem !important;
    padding: 0.5rem 1.25rem !important;
    box-shadow: 0 1px 4px rgba(79,70,229,.35) !important;
    transition: opacity 0.15s, transform 0.1s !important;
}
button[data-testid="baseButton-primary"]:hover {
    opacity: 0.92 !important; transform: translateY(-1px) !important;
    box-shadow: 0 4px 12px rgba(79,70,229,.45) !important;
}
button[data-testid="baseButton-secondary"] {
    border: 1.5px solid var(--neutral-200) !important;
    border-radius: var(--radius-sm) !important;
    font-weight: 500 !important; font-size: 0.875rem !important;
    background: white !important; color: var(--neutral-700) !important;
    transition: border-color 0.15s, color 0.15s !important;
}
button[data-testid="baseButton-secondary"]:hover {
    border-color: var(--primary) !important; color: var(--primary) !important;
}

/* ── Containers / cards ───────────────────────────────────────── */
[data-testid="stVerticalBlockBorderWrapper"] > div {
    border-radius: var(--radius-md) !important;
    border-color: var(--neutral-200) !important;
    box-shadow: var(--shadow-sm) !important;
    background: white !important;
}

/* ── Expanders ────────────────────────────────────────────────── */
[data-testid="stExpander"] {
    border: 1px solid var(--neutral-200) !important;
    border-radius: var(--radius-md) !important;
    background: white !important;
    box-shadow: var(--shadow-sm) !important;
}
[data-testid="stExpander"] summary {
    font-weight: 600 !important;
    color: var(--neutral-900) !important;
}

/* ── Data tables ──────────────────────────────────────────────── */
[data-testid="stDataFrame"] {
    border: 1px solid var(--neutral-200) !important;
    border-radius: var(--radius-md) !important;
    overflow: hidden !important;
}

/* ── Code / pre ───────────────────────────────────────────────── */
.stCode, pre {
    background: var(--neutral-100) !important;
    color: var(--neutral-900) !important;
    border: 1px solid var(--neutral-200) !important;
    border-radius: var(--radius-sm) !important;
    font-size: 0.82rem !important;
}
/* Override any Streamlit syntax-highlight container that forces dark BG */
[data-testid="stCode"] {
    background: var(--neutral-100) !important;
}
[data-testid="stCode"] pre,
[data-testid="stCode"] code {
    background: var(--neutral-100) !important;
    color: #1E293B !important;
}

/* ── Alerts ───────────────────────────────────────────────────── */
[data-testid="stAlert"] {
    border-radius: var(--radius-md) !important;
    border-left-width: 4px !important;
}

/* ── Form inputs ──────────────────────────────────────────────── */
[data-testid="stTextInput"] input,
[data-testid="stTextArea"] textarea,
[data-testid="stSelectbox"] > div > div {
    border-radius: var(--radius-sm) !important;
    border-color: var(--neutral-200) !important;
    font-size: 0.875rem !important;
}
[data-testid="stTextInput"] input:focus,
[data-testid="stTextArea"] textarea:focus {
    border-color: var(--primary) !important;
    box-shadow: 0 0 0 3px rgba(79,70,229,.15) !important;
}

/* ── Section headers inside tabs ──────────────────────────────── */
.ent-section-header {
    display: flex; align-items: center; gap: 10px;
    margin: 1.5rem 0 0.5rem; border-bottom: 2px solid var(--primary-xlight);
    padding-bottom: 8px;
}
.ent-section-header h3 {
    font-size: 1.05rem; font-weight: 700; color: var(--neutral-900); margin: 0;
}

/* ── Status badges ────────────────────────────────────────────── */
.badge {
    display: inline-block; padding: 2px 9px;
    border-radius: 999px; font-size: 0.72rem; font-weight: 700;
    letter-spacing: 0.04em; text-transform: uppercase;
}
.badge-active  { background:#D1FAE5; color:#065F46; }
.badge-draft   { background:#EEF2FF; color:#3730A3; }
.badge-warning { background:#FEF3C7; color:#92400E; }
.badge-danger  { background:#FFE4E6; color:#9F1239; }
.badge-neutral { background:#F1F5F9; color:#475569; }

/* ── Rule card ────────────────────────────────────────────────── */
.rule-card {
    background: white; border: 1px solid var(--neutral-200);
    border-radius: var(--radius-md); padding: 14px 18px;
    margin-bottom: 10px; box-shadow: var(--shadow-sm);
    display: flex; align-items: flex-start; gap: 14px;
}
.rule-card .rule-icon {
    width: 36px; height: 36px; border-radius: var(--radius-sm);
    background: var(--primary-xlight); display: flex; align-items: center;
    justify-content: center; font-size: 1.1rem; flex-shrink: 0;
}
.rule-card .rule-body { flex: 1; min-width: 0; }
.rule-card .rule-id { font-size: 0.75rem; font-weight: 600; color: var(--primary); font-family: monospace; }
.rule-card .rule-name { font-size: 0.95rem; font-weight: 700; color: var(--neutral-900); margin: 2px 0 4px; }
.rule-card .rule-desc { font-size: 0.82rem; color: #64748B; line-height: 1.5; }

/* ── Step card (guide) ────────────────────────────────────────── */
.step-card {
    background: white; border: 1px solid var(--neutral-200);
    border-radius: var(--radius-md); padding: 20px 22px 18px;
    box-shadow: var(--shadow-sm); position: relative;
}
.step-card .step-num {
    position: absolute; top: -14px; left: 20px;
    background: var(--primary); color: white; border-radius: 999px;
    width: 28px; height: 28px; display: flex; align-items: center;
    justify-content: center; font-size: 0.8rem; font-weight: 700;
}
.step-card .step-title { font-size: 1rem; font-weight: 700; color: var(--neutral-900); margin: 4px 0 8px; }
.step-card .step-body  { font-size: 0.875rem; color: #475569; line-height: 1.65; }

/* ── Workflow pill ────────────────────────────────────────────── */
.workflow-row {
    display: flex; align-items: center; gap: 0; flex-wrap: wrap;
    margin: 1.5rem 0;
}
.workflow-pill {
    background: var(--primary-xlight); color: var(--primary);
    border: 1.5px solid var(--primary-light);
    border-radius: 999px; padding: 6px 18px;
    font-size: 0.82rem; font-weight: 700; white-space: nowrap;
}
.workflow-arrow { color: var(--primary-light); font-size: 1.2rem; padding: 0 6px; }

/* ── Chat bubbles ─────────────────────────────────────────────── */
.chat-bubble-user {
    background: var(--primary); color: white;
    border-radius: 18px 18px 4px 18px;
    padding: 10px 14px; font-size: 0.875rem; max-width: 80%;
    margin-left: auto; margin-bottom: 8px; box-shadow: var(--shadow-sm);
}
.chat-bubble-assistant {
    background: white; color: var(--neutral-900);
    border: 1px solid var(--neutral-200);
    border-radius: 4px 18px 18px 18px;
    padding: 10px 14px; font-size: 0.875rem; max-width: 88%;
    margin-right: auto; margin-bottom: 8px; box-shadow: var(--shadow-sm);
}
.chat-avatar {
    width: 30px; height: 30px; border-radius: 50%; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: 0.75rem; font-weight: 700;
}
.chat-avatar-ai { background: var(--primary); color: white; }
.chat-avatar-user { background: #E2E8F0; color: var(--neutral-700); }
.chat-ts { font-size: 0.68rem; color: #94A3B8; margin-top: 3px; }

/* ── Quick-action chips ───────────────────────────────────────── */
.qa-chip-row { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 12px; }
.qa-chip {
    background: var(--primary-xlight); color: var(--primary);
    border: 1.5px solid var(--primary-light); border-radius: 999px;
    padding: 5px 14px; font-size: 0.78rem; font-weight: 600;
    cursor: pointer; transition: background 0.15s;
    white-space: nowrap;
}
.qa-chip:hover { background: var(--primary); color: white; }

/* ── Divider upgrade ──────────────────────────────────────────── */
hr { border-color: var(--neutral-200) !important; margin: 1.5rem 0 !important; }

/* ── Scrollbar ────────────────────────────────────────────────── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--neutral-100); }
::-webkit-scrollbar-thumb { background: var(--neutral-200); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #CBD5E1; }

/* ── Sidebar — white background, black text ───────────────────── */
[data-testid="stSidebar"] { background: #ffffff !important; }
[data-testid="stSidebar"] * { color: #111111 !important; }
[data-testid="stSidebar"] input,
[data-testid="stSidebar"] select,
[data-testid="stSidebar"] textarea { background: #f8f9fa !important; border-color: #dee2e6 !important; }
[data-testid="stSidebar"] button { color: #ffffff !important; }
</style>
""", unsafe_allow_html=True)

SOURCE_TYPES = ("postgresql", "mssql", "athena", "redshift")
_TYPE_MANUAL = "✏️  Type manually…"


# ---------------------------------------------------------------------------
# Flash-message / toast helper
# ---------------------------------------------------------------------------
# st.success(...) immediately followed by st.rerun() never actually shows —
# rerun tears down the current run before the message can render. The fix is
# to stash the message in session_state, rerun, and show it as a toast at the
# very top of the NEXT run (toasts persist briefly and are visible regardless
# of which tab is active, so it's obvious the save actually happened).

def flash(message: str, icon: str = "✅"):
    st.session_state["_flash"] = (message, icon)


def _show_pending_flash():
    pending = st.session_state.pop("_flash", None)
    if pending:
        message, icon = pending
        st.toast(message, icon=icon)


_show_pending_flash()


# ---------------------------------------------------------------------------
# Paginated CSV/DataFrame viewer — used anywhere a validation result set could
# be large (summary CSVs, row-level mismatch diffs) instead of dumping the
# whole thing into one st.dataframe.
# ---------------------------------------------------------------------------

def _style_status(df):
    """Colors a 'status' column (PASS/FAIL) green/red if present — purely cosmetic."""
    if "status" not in df.columns:
        return df
    def _color(v):
        if v == "PASS":
            return "color: #1a7f37; font-weight: 600"
        if v == "FAIL":
            return "color: #c0392b; font-weight: 600"
        return ""
    return df.style.map(_color, subset=["status"])


def _render_diff_file(f, key_prefix: str):
    """Render one row-level result CSV as a wide table.

    Every row is shown (PASS and FAIL).  The table has:
      col_varchar_normalized (index) | status | col_text (source) | col_text (target) | ...

    PASS rows are green, FAIL/SOURCE_ONLY/TARGET_ONLY rows are red/amber.
    Differing cells are highlighted yellow so they stand out in the wide view.
    """
    import pandas as pd
    import numpy as np

    try:
        df = pd.read_csv(f)
    except Exception as exc:
        st.warning(f"Could not read `{f.name}`: {exc}")
        return

    n_total = len(df)
    n_fail  = int((df["status"] != "PASS").sum()) if n_total else 0
    n_pass  = n_total - n_fail
    label   = f.stem.split("_result_")[0] if "_result_" in f.stem else f.stem

    # Identify data columns from __source/__target pairs
    src_col_keys = [c for c in df.columns if c.endswith("__source")]
    data_cols    = [c[: -len("__source")] for c in src_col_keys]

    # ── Build wide display DataFrame ─────────────────────────────────────────
    # Columns: row_key | status | col1 (source) | col1 (target) | col2 ...
    display_cols = {"row_key": df["row_key"], "status": df["status"]}
    final_col_names = ["row_key", "status"]

    for col in data_cols:
        short = col.removesuffix("_normalized") if col.endswith("_normalized") else col
        src_key = f"{col}__source"
        tgt_key = f"{col}__target"
        display_cols[f"{short} (source)"] = df[src_key].fillna("") if src_key in df.columns else ""
        display_cols[f"{short} (target)"] = df[tgt_key].fillna("") if tgt_key in df.columns else ""
        final_col_names += [f"{short} (source)", f"{short} (target)"]

    wide = pd.DataFrame(display_cols)[final_col_names]

    STATUS_BG = {"PASS": "#d4edda", "FAIL": "#f8d7da",
                 "SOURCE_ONLY": "#fff3cd", "TARGET_ONLY": "#fff3cd"}
    STATUS_FG = {"PASS": "#1a7f37", "FAIL": "#c0392b",
                 "SOURCE_ONLY": "#856404", "TARGET_ONLY": "#856404"}

    def _style_wide(df_in):
        styles = pd.DataFrame("", index=df_in.index, columns=df_in.columns)
        for i, row in df_in.iterrows():
            row_status = row["status"]
            bg = STATUS_BG.get(row_status, "")
            # Colour the whole row with the row-level status background
            styles.loc[i, :] = f"background-color: {bg}"
            # Override status cell text colour
            styles.loc[i, "status"] = (
                f"background-color: {bg}; color: {STATUS_FG.get(row_status, '')}; font-weight: 700"
            )
            # Highlight individual cells that differ (yellow) only for FAIL rows
            if row_status not in ("PASS",):
                for col in data_cols:
                    short = col.removesuffix("_normalized") if col.endswith("_normalized") else col
                    sc, tc = f"{short} (source)", f"{short} (target)"
                    if sc in df_in.columns and tc in df_in.columns:
                        sv = str(row.get(sc, ""))
                        tv = str(row.get(tc, ""))
                        if sv != tv:
                            styles.loc[i, sc] = f"background-color: #fff3cd; color: #856404"
                            styles.loc[i, tc] = f"background-color: #fff3cd; color: #856404"
        return styles

    with st.expander(
        f"**{label}** — {n_fail} row(s) FAIL / {n_pass} PASS out of {n_total}",
        expanded=True,
    ):
        m1, m2, m3 = st.columns(3)
        m1.metric("Rows compared", n_total)
        m2.metric("Passed", n_pass)
        m3.metric("Failed", n_fail, delta=-n_fail if n_fail else None, delta_color="inverse")

        st.caption(
            "Each row shows source (PostgreSQL) and target (Snowflake) values side-by-side. "
            "Green = full row matched · Red = mismatch · Yellow cell = the specific value that differed."
        )
        st.dataframe(
            wide.style.apply(_style_wide, axis=None),
            use_container_width=True,
            hide_index=True,
        )


def render_paginated_df(df, key_prefix: str, page_size_options=(10, 25, 50, 100), style_status: bool = True):
    """Renders a DataFrame with page-size + page-number controls instead of
    one long scrollable table. Returns nothing — renders directly."""
    n_rows = len(df)
    if n_rows == 0:
        st.caption("No rows.")
        return

    pc1, pc2 = st.columns([1, 3])
    with pc1:
        page_size = st.selectbox(
            "Rows per page", page_size_options,
            index=min(1, len(page_size_options) - 1), key=f"{key_prefix}_page_size",
        )
    n_pages = max((n_rows + page_size - 1) // page_size, 1)
    with pc2:
        page = st.number_input(
            f"Page (1–{n_pages})", min_value=1, max_value=n_pages, value=1, step=1,
            key=f"{key_prefix}_page",
        )
    start, end = (page - 1) * page_size, min(page * page_size, n_rows)
    st.caption(f"Showing rows {start + 1}–{end} of {n_rows}")

    page_df = df.iloc[start:end]
    st.dataframe(_style_status(page_df) if style_status else page_df, width='stretch', hide_index=True)


# ---------------------------------------------------------------------------
# Cached discovery calls — one live query per (host, creds, ...) combo, not
# re-run on every widget interaction elsewhere on the page.
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner="Discovering databases…")
def cached_source_databases(db_type, host, port, username, password, auth):
    if db_type == "postgresql":
        return _discover_postgres_databases(host, port, username, password)
    if db_type == "mssql":
        return _discover_mssql_databases(host, port, username, password, auth)
    return []  # Athena: database == fixed Glue database, no server-side listing


@st.cache_data(ttl=300, show_spinner="Discovering schemas…")
def cached_source_schemas(db_type, host, port, database, username, password, auth):
    if db_type == "postgresql":
        return _discover_postgres_schemas(host, port, database, username, password)
    if db_type == "mssql":
        return _discover_mssql_schemas(host, port, database, username, password, auth)
    return []  # Athena: schema == database, no separate listing


@st.cache_data(ttl=300, show_spinner="Loading tables…")
def cached_source_tables(db_type, host, port, database, username, password, auth, s3_output, schema):
    extractor = ExtractorFactory.create(
        db_type, host=host, port=port, database=database,
        username=username, password=password, auth=auth, s3_output=s3_output,
    )
    return extractor.list_tables(schema)


@st.cache_data(ttl=300, show_spinner="Loading columns…")
def cached_source_columns(db_type, host, port, database, username, password, auth, s3_output, schema, table):
    extractor = ExtractorFactory.create(
        db_type, host=host, port=port, database=database,
        username=username, password=password, auth=auth, s3_output=s3_output,
    )
    return [c.column_name for c in extractor.extract_columns(schema, table)]


@st.cache_data(ttl=300, show_spinner="Discovering Snowflake databases…")
def cached_sf_databases(account, username, password, warehouse, role):
    return _discover_snowflake_databases(account, username, password, warehouse, role)


@st.cache_data(ttl=300, show_spinner="Discovering Snowflake schemas…")
def cached_sf_schemas(account, database, username, password, warehouse, role):
    rows = _discover_snowflake_schemas(account, database, username, password, warehouse, role)
    return [r[0] for r in rows]


@st.cache_data(ttl=300, show_spinner="Loading Snowflake tables…")
def cached_sf_tables(database, schema):
    return SnowflakeExtractor(database=database).list_tables(schema)


@st.cache_data(ttl=300, show_spinner="Loading Snowflake columns…")
def cached_sf_columns(database, schema, table):
    return [c.column_name for c in SnowflakeExtractor(database=database).extract_columns(schema, table)]


@st.cache_data(ttl=300, show_spinner=False)
def cached_sf_column_types(database, schema, table):
    """{column_name: data_type} for the Snowflake target — used to fill in the
    target type when persisting a human-corrected column mapping as a learned
    example (see render_mapping_review save button)."""
    return {c.column_name: c.data_type for c in SnowflakeExtractor(database=database).extract_columns(schema, table)}


# ---------------------------------------------------------------------------
# Filter history — read from existing plan JSONs, no extra storage needed
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def load_filter_history() -> dict:
    """
    Scan all persisted plan JSON files and return previously-used migration
    filters keyed by source table name.

    Returns: {table_name: [(source_filter, target_filter), ...]}
    Only includes entries where at least source_filter is non-empty.
    Deduplicates within a table. Most-recently-used first.
    """
    plans_root = _ROOT_DIR / "output" / "plans"
    seen: dict = {}   # table -> list of (src_filter, tgt_filter), insertion order = newest first
    if not plans_root.exists():
        return {}
    for plan_file in sorted(plans_root.rglob("*.plan.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            import json as _json
            data = _json.loads(plan_file.read_text(encoding="utf-8"))
            src_filter = data.get("source_filter", "").strip()
            tgt_filter = data.get("target_filter", "").strip()
            if not src_filter:
                continue
            table = data.get("source", {}).get("table") or data.get("source_table", "")
            if not table:
                continue
            pair = (src_filter, tgt_filter)
            if table not in seen:
                seen[table] = []
            if pair not in seen[table]:
                seen[table].append(pair)
        except Exception:
            continue
    return seen


def filter_options_for(table: str) -> list:
    """
    Return previously-used (source_filter, target_filter) pairs for a table.
    Empty list means no history exists yet.
    """
    return load_filter_history().get(table, [])


# ---------------------------------------------------------------------------
# Shared UI helpers
# ---------------------------------------------------------------------------

def load_registry() -> list:
    """Configured source connections from .env, same as the CLI's picker."""
    try:
        registry = print_connection_registry(_ROOT_DIR / ".env")
    except Exception as exc:
        st.error(f"Could not read .env connections: {exc}")
        return []
    out = []
    for rec in registry:
        rec = dict(rec)
        rec["db_type"] = _normalize_db_type(rec["db_type"])
        rec = _apply_database_registry(rec)
        out.append(rec)
    return out


def connection_label(rec: dict) -> str:
    label = _DB_TYPE_LABELS.get(rec["db_type"], rec["db_type"])
    return f"SRC_{rec['index']}  ·  {label}  ·  {rec['host']}/{rec['database']}.{rec['schema']}"


def select_connection(registry: list, key: str):
    if not registry:
        st.warning("No source connections found in .env. Run the setup wizard first: `python src/validate_cli.py setup`")
        return None
    options = {connection_label(r): r for r in registry}
    chosen = st.selectbox("Source connection", list(options.keys()), key=key)
    return options[chosen]


def select_or_type(label: str, options: list, default: str, key: str, format_func=None) -> str:
    """A dropdown of live-discovered values, with a fallback to type a value
    manually (discovery can fail — driver missing, permissions, brand-new
    table not created yet, etc.) so the picker never becomes a dead end."""
    opts = list(dict.fromkeys(options))  # de-dupe, preserve order
    if default and default not in opts:
        opts = [default] + opts
    display_opts = opts + [_TYPE_MANUAL]
    default_idx = display_opts.index(default) if default in display_opts else 0
    kwargs = {"format_func": format_func} if format_func else {}
    choice = st.selectbox(label, display_opts, index=default_idx, key=f"{key}_sel", **kwargs)
    if choice == _TYPE_MANUAL:
        return st.text_input(f"{label} (type manually)", value=default or "", key=f"{key}_txt")
    return choice


@st.cache_data(ttl=300, show_spinner="Checking which AI models are reachable…")
def available_models_for_ui() -> list:
    """Dynamic model list — probes DIAL for reachability when a key is set,
    otherwise returns the full curated registry so the picker still works."""
    api_key = os.getenv("DIAL_API_KEY", "")
    if not api_key:
        return list(AVAILABLE_MODELS)
    try:
        working = get_working_models(
            AVAILABLE_MODELS, api_key,
            os.getenv("DIAL_API_BASE", ""), os.getenv("DIAL_API_VERSION", ""),
        )
        return working or list(AVAILABLE_MODELS)
    except Exception:
        return list(AVAILABLE_MODELS)


def _model_label(model_id: str) -> str:
    if model_id == _TYPE_MANUAL:
        return model_id
    info = MODEL_DESCRIPTIONS.get(model_id)
    if not info:
        return model_id
    vendor, display_name, description = info
    return f"{display_name}  ·  {vendor} — {description}"


def source_password(rec: dict) -> str:
    return os.getenv(f"{rec['prefix']}PASSWORD", "")


_LAYERS = ("bronze", "silver", "gold")


def pick_layer(key: str) -> tuple:
    """Medallion layer picker — mirrors the CLI's interactive '1) bronze
    2) silver 3) gold' prompt (validate_cli.py), which only ever ran in the
    terminal flow. Returns (layer_name, output_dir) where output_dir is where
    the generated YAML/SQL config files are written."""
    layer = st.selectbox(
        "Medallion layer — where to write the generated config",
        _LAYERS, index=0, key=key,
        help="Controls only the output folder for generated YAML/SQL configs (Project/config/<layer>/), "
             "not which Snowflake database/schema is queried — that's chosen above.",
    )
    return layer, _ROOT_DIR / "Project" / "config" / layer


def snowflake_creds() -> dict:
    return {
        "account":   os.getenv("SNOWFLAKE_ACCOUNT", ""),
        "username":  os.getenv("SNOWFLAKE_USERNAME", ""),
        "password":  os.getenv("SNOWFLAKE_PASSWORD", ""),
        "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE", ""),
        "role":      os.getenv("SNOWFLAKE_ROLE", ""),
        "database":  os.getenv("SNOWFLAKE_DATABASE", ""),
        "schema":    os.getenv("SNOWFLAKE_SCHEMA", ""),
    }


def pick_source_location(rec: dict, key_prefix: str):
    """Cascading database -> schema -> table picker for a source connection,
    all live-discovered using the credentials already in .env. Returns
    (database, schema, table_options, chosen_table_or_None)."""
    db_type  = rec["db_type"]
    password = source_password(rec)

    if db_type == "athena":
        st.caption(f"Athena database/schema is fixed to the Glue database configured in .env: **{rec['database']}**")
        database = rec["database"]
        schema   = rec["schema"]
    else:
        databases = cached_source_databases(db_type, rec["host"], int(rec.get("port") or 0), rec["username"], password, rec.get("auth", ""))
        c1, c2 = st.columns(2)
        with c1:
            database = select_or_type("Source database", databases, rec["database"], f"{key_prefix}_db")
        schemas = cached_source_schemas(db_type, rec["host"], int(rec.get("port") or 0), database, rec["username"], password, rec.get("auth", ""))
        with c2:
            schema = select_or_type("Source schema", schemas, rec["schema"], f"{key_prefix}_schema")

    try:
        tables = cached_source_tables(
            db_type, rec["host"], int(rec.get("port") or 0), database,
            rec["username"], password, rec.get("auth", ""), rec.get("s3_output", ""), schema,
        )
    except Exception as exc:
        st.error(f"Could not list tables: {exc}")
        tables = []

    return database, schema, tables


def pick_snowflake_target(default_table: str, key_prefix: str, include_table: bool = True):
    """Cascading database -> schema -> table picker for the Snowflake target,
    live-discovered the same way as the source picker.

    `include_table` controls whether a single Snowflake table dropdown is
    shown — used for the Single YAML flow, which maps one table directly.
    The Batch YAML flow maps each source table to its own target in the
    per-table mapping grid instead, so it skips this (set include_table=False)
    to avoid a confusing, unused single-table dropdown."""
    creds = snowflake_creds()
    databases = cached_sf_databases(creds["account"], creds["username"], creds["password"], creds["warehouse"], creds["role"])
    c1, c2 = st.columns(2)
    with c1:
        sf_database = select_or_type("Snowflake database", databases, creds["database"], f"{key_prefix}_sfdb")
    schemas = cached_sf_schemas(creds["account"], sf_database, creds["username"], creds["password"], creds["warehouse"], creds["role"])
    with c2:
        sf_schema = select_or_type("Snowflake schema", schemas, creds["schema"], f"{key_prefix}_sfschema")

    if not include_table:
        return sf_database, sf_schema, None

    try:
        sf_tables = cached_sf_tables(sf_database, sf_schema)
    except Exception as exc:
        st.error(f"Could not list Snowflake tables: {exc}")
        sf_tables = []

    sf_table = select_or_type("Snowflake table", sf_tables, default_table, f"{key_prefix}_sftable")
    return sf_database, sf_schema, sf_table


def render_mapping_review(
    pipeline,
    pg_schema: str, pg_table: str, sf_schema: str, sf_table: str,
    sf_database: str, pg_database: str,
    exclude_columns: list, key_prefix: str,
) -> dict:
    """
    Preview the source→target COLUMN mapping the AI/fuzzy matcher would use,
    and let a human correct it before anything is generated.

    The AI mapper can flag a rename as a mismatch even when it's correct
    (e.g. source 'customer' -> target 'user'), and can just as easily auto-
    accept a wrong guess. This grid shows exactly how every column was
    matched (method + confidence) so a human can fix it either way — for a
    single table or, called once per table, for a batch run.

    Returns:
        {source_column: corrected_target_column} for every row the human
        edited away from the AI/fuzzy suggestion. Empty dict if nothing was
        previewed or nothing was changed — callers pass this straight in as
        explicit_mappings, so an empty dict behaves exactly like "no override".
    """
    rows_key = f"{key_prefix}_mapping_rows"
    sig_key = f"{key_prefix}_mapping_sig"
    sig = (pg_schema, pg_table, sf_schema, sf_table, sf_database, pg_database, tuple(sorted(exclude_columns or [])))

    if st.button("🔍 Preview column mapping", key=f"{key_prefix}_preview_btn"):
        with st.spinner(f"Matching columns for {pg_table} → {sf_table} ..."):
            try:
                st.session_state[rows_key] = pipeline.preview_mapping(
                    pg_schema=pg_schema, pg_table=pg_table,
                    sf_schema=sf_schema, sf_table=sf_table,
                    sf_database=sf_database, pg_database=pg_database,
                    exclude_columns=exclude_columns,
                )
                st.session_state[sig_key] = sig
            except Exception as exc:
                st.error(f"Column mapping preview failed: {exc}")
                st.session_state.pop(rows_key, None)

    rows = st.session_state.get(rows_key)
    if rows is None or st.session_state.get(sig_key) != sig:
        st.caption(
            "Not previewed yet — click above to see how each source column was matched "
            "to a target column, and correct any that are wrong."
        )
        return {}

    try:
        live_tgt_cols = cached_sf_columns(sf_database, sf_schema, sf_table)
    except Exception:
        live_tgt_cols = []

    import pandas as pd
    df = pd.DataFrame([{
        "Source Column": r["source_column"],
        "Source Type": r["source_type"],
        "AI/Fuzzy Target": r["target_column"],
        "Corrected Target": r["target_column"],
        "Matched By": "skip" if r["skip_validation"] else r["match_method"],
        "Confidence": r["confidence"],
    } for r in rows])

    target_options = sorted(set(live_tgt_cols) | {r["target_column"] for r in rows if r["target_column"]})

    edited = st.data_editor(
        df,
        column_config={
            "Source Column": st.column_config.TextColumn(disabled=True),
            "Source Type": st.column_config.TextColumn(disabled=True),
            "AI/Fuzzy Target": st.column_config.TextColumn(
                disabled=True, help="What the AI/fuzzy matcher picked — kept for comparison.",
            ),
            "Corrected Target": st.column_config.SelectboxColumn(
                options=target_options + [""],
                help="Override the mapping if it's wrong in either direction — the mapper "
                     "may have missed a valid rename (e.g. 'customer' -> 'user') or accepted a wrong guess.",
            ),
            "Matched By": st.column_config.TextColumn(disabled=True),
            "Confidence": st.column_config.NumberColumn(disabled=True, format="%.2f"),
        },
        hide_index=True,
        width='stretch',
        key=f"{key_prefix}_mapping_editor",
    )

    # corrected_targets: columns the user has manually fixed in the grid this session
    corrected_targets = {
        row["Source Column"]
        for _, row in edited.iterrows()
        if row["Corrected Target"] and row["Corrected Target"] != row["AI/Fuzzy Target"]
    }

    # skipped = skip_validation=True AND no user correction yet
    skipped_low = [
        r for r in rows
        if r.get("skip_validation")
        and not r.get("target_column")
        and r["source_column"] not in corrected_targets
        and r.get("confidence", 0.0) < 0.75
    ]
    # matched but low confidence (not corrected, not a learned rule)
    low_conf_rows = [
        r for r in rows
        if not r.get("skip_validation")
        and r.get("target_column")
        and r["source_column"] not in corrected_targets
        and r.get("confidence", 1.0) < 0.75
    ]

    if skipped_low:
        _sk_cols = ", ".join(
            f"`{r['source_column']}` ({int(r.get('confidence',0)*100)}%)" for r in skipped_low
        )
        _sk_c1, _sk_c2 = st.columns([3, 1])
        with _sk_c1:
            st.warning(
                f"⚠️ **{len(skipped_low)} column(s) skipped — no target match found** "
                f"(confidence below 75%): {_sk_cols}\n\n"
                "Set a **Corrected Target** above to include them, or raise a Jira ticket for manual review."
            )
        with _sk_c2:
            if st.button("🎫 Raise Jira ticket", key=f"{key_prefix}_skip_jira_btn"):
                try:
                    from connector.jira_client import create_ticket, is_configured
                    if not is_configured():
                        st.info("Jira not configured — set `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY` in `.env`.")
                    else:
                        _t = create_ticket(
                            summary=f"[Migration Validator] Skipped columns need mapping: {pg_table}",
                            description=(
                                f"Table: {pg_table}\n\nColumns skipped (no target match, confidence <75%):\n"
                                + "\n".join(
                                    f"  - {r['source_column']} (conf {int(r.get('confidence',0)*100)}%,"
                                    f" reason: {r.get('skip_reason','unknown')})"
                                    for r in skipped_low
                                )
                            ),
                            labels=["migration-validator", "skipped-columns", "needs-review"],
                        )
                        st.success(f"Jira ticket created: [{_t['key']}]({_t['url']})")
                except Exception as _ske:
                    st.error(f"Jira error: {_ske}")

    unmatched = [r["source_column"] for r in rows if not r.get("skip_validation") and not r.get("target_column")]
    if unmatched:
        st.warning(f"No target match for: {', '.join(unmatched)} — pick one above or it will be skipped from validation.")
    if low_conf_rows:
        _lc_col1, _lc_col2 = st.columns([3, 1])
        with _lc_col1:
            st.info(f"Matched below high confidence: {', '.join(r['source_column'] for r in low_conf_rows)} — review these; a flagged mismatch can still be correct.")
        with _lc_col2:
            if st.button("🎫 Raise Jira ticket", key=f"{key_prefix}_inline_jira_btn"):
                try:
                    from connector.jira_client import create_ticket, is_configured
                    if not is_configured():
                        st.info("Jira not configured — set `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY` in `.env`.")
                    else:
                        _t = create_ticket(
                            summary=f"[Migration Validator] Low-confidence mappings: {pg_table}",
                            description=(
                                f"Table: {pg_table}\n\nLow-confidence column mappings:\n"
                                + "\n".join(f"  - {r['source_column']} → {r['target_column']} ({int(r['confidence']*100)}%)" for r in low_conf_rows)
                            ),
                            labels=["migration-validator", "needs-review"],
                        )
                        st.success(f"Jira ticket created: [{_t['key']}]({_t['url']})")
                except Exception as _lce:
                    st.error(f"Jira error: {_lce}")

    overrides = {
        row["Source Column"]: row["Corrected Target"]
        for _, row in edited.iterrows()
        if row["Corrected Target"] and row["Corrected Target"] != row["AI/Fuzzy Target"]
    }
    if overrides:
        st.success(
            f"{len(overrides)} correction(s) will be forced onto the mapping: "
            + ", ".join(f"{k} → {v}" for k, v in overrides.items())
        )
        if st.button("💾 Remember these corrections for future runs", key=f"{key_prefix}_save_corrections"):
            try:
                tgt_types = cached_sf_column_types(sf_database, sf_schema, sf_table)
            except Exception:
                tgt_types = {}
            src_type_by_col = {r["source_column"]: r["source_type"] for r in rows}

            def _rule_id_for(src_type: str, tgt_type: str) -> str:
                from rules import get_rule_for_type
                try:
                    return get_rule_for_type(src_type, tgt_type).rule_name
                except Exception:
                    return "text"

            recorder = FeedbackRecorder()
            saved = recorder.record_batch([
                MismatchFeedback(
                    source_column=src_col,
                    target_column=corrected_tgt,
                    source_type=src_type_by_col.get(src_col, ""),
                    target_type=tgt_types.get(corrected_tgt, ""),
                    correct_rule=_rule_id_for(src_type_by_col.get(src_col, ""), tgt_types.get(corrected_tgt, "")),
                    reason="Corrected via webapp column mapping review",
                    table_name=pg_table,
                    was_ai_decision=True,
                )
                for src_col, corrected_tgt in overrides.items()
            ])
            st.success(f"Saved {saved} correction(s) to rule_book_learned.json — future runs will recognize them.")
    return overrides


def render_custom_sql_section(
    mapping_rows: list,
    src_db_type: str,
    src_table_fqn: str,
    sf_table_fqn: str,
    output_dir,
    default_filename: str,
    key_prefix: str,
) -> None:
    """
    Optional 'ask AI for extra SQL' panel, scoped to one table's already-
    reviewed column mapping (name, type, target counterpart).

    Lives right after the mapping-review grid (both in the single-table flow
    and per-table inside batch) rather than as a standalone screen, because
    it needs a concrete, human-reviewed column list to give the AI accurate
    context — that data only exists once a mapping preview has been run for
    this specific table. A standalone version would just be this same form
    asking the user to type out columns/types by hand instead of reusing
    what's already been reviewed on screen.
    """
    with st.expander("🧪 Generate custom SQL from a prompt (optional)", expanded=False):
        st.caption(
            "Ask for any SQL beyond the standard source/target validation query — "
            "e.g. a dedup check, or a query with an extra business condition. "
            "Scoped to whichever columns you keep checked below."
        )

        import pandas as pd
        from rules import get_rule_for_type

        def _rule_name(r: dict) -> str:
            try:
                return get_rule_for_type(r["source_type"], r["target_type"] or r["source_type"]).rule_name
            except Exception:
                return "text"

        usable_rows = [r for r in mapping_rows if not r["skip_validation"]]
        cols_df = pd.DataFrame([{
            "Use": True,
            "Source column": r["source_column"],
            "Source type": r["source_type"],
            "Target column": r["target_column"] or "(unmatched)",
            "Target type": r["target_type"],
        } for r in usable_rows])

        picked = st.data_editor(
            cols_df,
            column_config={
                "Use": st.column_config.CheckboxColumn(help="Include this column as context for the AI."),
                "Source column": st.column_config.TextColumn(disabled=True),
                "Source type": st.column_config.TextColumn(disabled=True),
                "Target column": st.column_config.TextColumn(disabled=True),
                "Target type": st.column_config.TextColumn(disabled=True),
            },
            hide_index=True, width='stretch', key=f"{key_prefix}_custom_cols_editor",
        )

        source_label = f"Source only ({src_db_type})"
        target_choice = st.radio(
            "Generate SQL for:",
            options=[source_label, "Snowflake only", "Both sides"],
            horizontal=True, key=f"{key_prefix}_custom_target_choice",
            help="Column names/types often differ between source and target — pick 'Both sides' "
                 "to get two separate, correctly-named queries rather than one query with mismatched names.",
        )

        custom_prompt = st.text_area(
            "What should this query do?",
            placeholder="e.g. Find duplicate employee names, ignoring case and whitespace",
            key=f"{key_prefix}_custom_prompt",
        )

        custom_model = select_or_type(
            "AI model for this query", available_models_for_ui(),
            os.getenv("DIAL_MODEL", "gpt-4o"),
            f"{key_prefix}_custom_model", format_func=_model_label,
        )

        sql_key = f"{key_prefix}_custom_sql"

        if st.button("✨ Generate SQL", key=f"{key_prefix}_custom_generate"):
            selected_names = {row["Source column"] for _, row in picked.iterrows() if row["Use"]}
            selected = [r for r in usable_rows if r["source_column"] in selected_names]
            if not custom_prompt.strip():
                st.error("Describe what the query should do first.")
            elif not selected:
                st.error("Select at least one column.")
            else:
                want_source = target_choice != "Snowflake only"
                want_target = target_choice != source_label
                results = {}
                with st.spinner("Generating SQL..."):
                    gen = AISQLQueryGenerator(model=custom_model)
                    try:
                        if want_source:
                            src_cols = [
                                {"column": r["source_column"], "type": r["source_type"], "rule": _rule_name(r)}
                                for r in selected
                            ]
                            results["source"] = gen.generate_custom_query(
                                custom_prompt, src_table_fqn, src_cols, src_db_type,
                            ).query
                        if want_target:
                            tgt_cols = [
                                {"column": r["target_column"], "type": r["target_type"], "rule": _rule_name(r)}
                                for r in selected if r["target_column"]
                            ]
                            if not tgt_cols:
                                raise AISQLGenerationError(
                                    "None of the selected columns have a mapped target column — "
                                    "pick a different set, or review the mapping above first."
                                )
                            results["target"] = gen.generate_custom_query(
                                custom_prompt, sf_table_fqn, tgt_cols, "snowflake",
                            ).query
                        st.session_state[sql_key] = results
                    except AISQLGenerationError as exc:
                        st.error(f"Generation failed: {exc}")
                        st.session_state.pop(sql_key, None)

        results = st.session_state.get(sql_key)
        if results:
            if "source" in results:
                st.markdown(f"**Source SQL ({src_db_type}):**")
                st.code(results["source"], language="sql")
            if "target" in results:
                st.markdown("**Snowflake SQL:**")
                st.code(results["target"], language="sql")
            dl1, dl2 = st.columns(2)
            if dl1.button("🔄 Regenerate", key=f"{key_prefix}_custom_regen"):
                st.session_state.pop(sql_key, None)
                st.rerun()
            if dl2.button("💾 Save to file(s)", key=f"{key_prefix}_custom_save"):
                save_dir = Path(output_dir) if output_dir else Path("output")
                save_dir.mkdir(parents=True, exist_ok=True)
                saved = []
                if "source" in results:
                    fname = save_dir / f"{default_filename}_source_custom_query.sql"
                    fname.write_text(results["source"], encoding="utf-8")
                    saved.append(str(fname))
                if "target" in results:
                    fname = save_dir / f"{default_filename}_snowflake_custom_query.sql"
                    fname.write_text(results["target"], encoding="utf-8")
                    saved.append(str(fname))
                flash(f"Saved: {', '.join(saved)}", icon="💾")


# ---------------------------------------------------------------------------
# Sidebar — environment status, always visible regardless of active tab
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### Migration Validator")

    _dial_key = os.getenv("DIAL_API_KEY", "")
    _claude_key = os.getenv("CLAUDE_API_KEY", "")
    if _dial_key:
        st.success(f"AI backend: DIAL ({os.getenv('DIAL_MODEL', 'gpt-4o')})", icon="🤖")
    elif _claude_key:
        st.success(f"AI backend: Claude ({os.getenv('CLAUDE_MODEL', 'claude-3-5-sonnet-20241022')})", icon="🤖")
    else:
        st.error("No AI backend configured (DIAL_API_KEY / CLAUDE_API_KEY missing)", icon="⚠️")

    _sf_account = os.getenv("SNOWFLAKE_ACCOUNT", "")
    if _sf_account:
        st.info(f"Snowflake: {_sf_account}", icon="❄️")
    else:
        st.warning("Snowflake account not configured", icon="⚠️")

    st.divider()
    st.caption("Live dropdowns (databases/schemas/tables) are cached for 5 minutes.")
    if st.button("🔄 Refresh discovery cache", width='stretch'):
        st.cache_data.clear()
        flash("Discovery cache cleared — dropdowns will re-query live data.", icon="🔄")
        st.rerun()

    st.divider()
    with st.expander("🔌 Connections", expanded=False):
        _sidebar_registry = load_registry()
        if not _sidebar_registry:
            st.caption("No SRC_N_* connections found. Configure `.env` or run `python src/validate_cli.py setup`.")
        else:
            st.dataframe(
                [
                    {
                        "Slot": f"SRC_{r['index']}",
                        "Type": _DB_TYPE_LABELS.get(r["db_type"], r["db_type"]),
                        "Host": r["host"],
                        "Database": r["database"],
                        "Schema": r["schema"],
                    }
                    for r in _sidebar_registry
                ],
                width='stretch', hide_index=True,
            )

        _sf = snowflake_creds()
        st.caption(f"Snowflake target: **{_sf['database'] or 'not set'}**.{_sf['schema'] or 'not set'}")

        if st.button("🔎 Test all connections", width='stretch'):
            results = []
            for rec in _sidebar_registry:
                try:
                    extractor = _make_source_extractor(rec)
                    extractor.list_tables(rec["schema"])
                    results.append((connection_label(rec), True, ""))
                except Exception as exc:
                    results.append((connection_label(rec), False, str(exc)))
            try:
                sf_ext = SnowflakeExtractor(database=_sf["database"])
                sf_ext.list_tables(_sf["schema"])
                results.append(("Snowflake (target)", True, ""))
            except Exception as exc:
                results.append(("Snowflake (target)", False, str(exc)))

            for label, ok, err in results:
                if ok:
                    st.success(f"✓ {label}")
                else:
                    st.error(f"✗ {label} — {err}")

    st.divider()
    _sb_recs = _load_token_records()
    _sb_pricing = _load_pricing()
    if _sb_recs:
        _sb_tokens = sum(r.get("total_tokens", 0) for r in _sb_recs)
        _sb_cost = sum(
            _cost_for(r.get("model", ""), r.get("prompt_tokens", 0), r.get("completion_tokens", 0), _sb_pricing)
            for r in _sb_recs
        )
        st.markdown(
            f"**AI usage (all-time):** {len(_sb_recs):,} calls · "
            f"{_sb_tokens:,} tokens · **${_sb_cost:.4f}**"
        )
    else:
        st.caption("No AI calls logged yet.")


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.markdown("""
<div style="display:flex;align-items:center;gap:16px;padding:20px 0 8px;">
    <div style="width:48px;height:48px;border-radius:12px;
                background:linear-gradient(135deg,#4F46E5 0%,#818CF8 100%);
                display:flex;align-items:center;justify-content:center;
                font-size:1.5rem;box-shadow:0 4px 12px rgba(79,70,229,.35);flex-shrink:0;">🔷</div>
    <div>
        <div style="font-size:1.55rem;font-weight:800;color:#0F172A;letter-spacing:-0.02em;line-height:1.1;">
            Migration Validator</div>
        <div style="font-size:0.82rem;color:#64748B;margin-top:3px;font-weight:500;">
            PostgreSQL · MSSQL · Athena &nbsp;→&nbsp; Snowflake &nbsp;|&nbsp;
            AI-powered column mapping &nbsp;|&nbsp; Governed approval workflow
        </div>
    </div>
</div>
""", unsafe_allow_html=True)

tab_batch, tab_custom, tab_execute, tab_history, tab_rules, tab_excl, tab_jira, tab_usage, tab_guide, tab_files = st.tabs(
    ["📋 Generate YAMLs",
     "✍️ Custom SQL Validation",
     "🚀 Run Validation", "📈 History & Trends",
     "📖 Rule Book", "🚫 Exclusions", "🎫 My Jira Tickets", "💰 Usage & Cost", "📘 Guide",
     "📂 Output Files"]
)
# 📊 Usage & Cost lives in the sidebar (see below); 🤖 Gemini Chat is a
# floating widget (see end of file).

# =============================================================================
# TAB: Generate — Batch YAML
# =============================================================================
with tab_batch:
    st.subheader("Generate validation YAML for multiple tables")
    st.caption("Map each source table to one Snowflake target. Review every mapping, set table scope, then generate all configs together.")
    st.info("Workflow: **1. Select tables**  →  **2. Map targets**  →  **3. Review columns**  →  **4. Set scope**  →  **5. Generate all**", icon="🧭")
    _batch_mode = st.radio(
        "Choose batch workflow",
        ["📋 Standard (table mapping)", "📊 Report Pack (Excel)"],
        horizontal=True,
        key="batch_mode_radio",
        help="Standard creates one validation YAML per source table. Report Pack parses an uploaded Excel "
             "report and shows an editable AI-derived plan (tables/grain/filter) per row before generating.",
    )

    registry = load_registry()
    rec = select_connection(registry, key="batch_conn")

    if rec and _batch_mode == "📋 Standard (table mapping)":
        _override_source_env(rec)
        src_db_type = rec["db_type"]

        with st.container(border=True):
            st.markdown("### 1. Select source tables")
            st.caption("Choose multiple source tables from one connection. Each selected table becomes one validation job.")
            database, schema, table_options = pick_source_location(rec, "batch")
            source_tables = st.multiselect(
                "Source tables to validate",
                options=table_options,
                key="batch_tables_select",
            )
            if not table_options:
                st.caption("Table list unavailable — type names manually below (comma-separated).")
                manual_raw = st.text_input("Table names (comma-separated)", key="batch_tables_manual")
                source_tables = [t.strip() for t in manual_raw.split(",") if t.strip()]

        with st.container(border=True):
            st.markdown("### 2. Choose Snowflake target area")
            st.caption("Select target database and schema. You will map each source table below.")
            sf_database, sf_schema, _ = pick_snowflake_target("", "batch", include_table=False)

            sf_tables_live = []
            if sf_database and sf_schema:
                try:
                    sf_tables_live = cached_sf_tables(sf_database, sf_schema)
                except Exception as exc:
                    st.warning(f"Could not list Snowflake tables for mapping: {exc}")

        target_map: dict = {}
        ambiguous_tables: set = set()
        if source_tables:
            st.markdown("### 3. Map source tables to Snowflake targets")
            st.caption("Green or confirmed matches are suggestions. Resolve every warning manually before generation.")

            creds = snowflake_creds()
            confirmed_mappings = {}
            if sf_database and sf_schema and creds["account"]:
                confirmed_mappings = mapping_store.load_confirmed_mappings(
                    creds["account"], creds["username"], creds["password"], sf_database, sf_schema,
                )

            # Suggest a target the same way the CLI does: exact upper() match,
            # else closest fuzzy match, else the plain upper() guess. A source
            # table is flagged ambiguous when 2+ Snowflake tables are equally
            # plausible (e.g. ADDRESS vs ADDRESSES) — those are left blank so
            # a person has to pick, instead of silently guessing wrong.
            upper_sf = {t.upper(): t for t in sf_tables_live}

            def suggest(src_table: str) -> tuple:
                """Returns (suggested_target, status) where status is one of:
                confirmed / exact / ambiguous / not_found / no_data. Only
                'exact' and 'confirmed' are safe to silently pre-fill — every
                other status leaves the target blank so a human has to pick,
                rather than guessing a Snowflake table name that may not
                exist (the ACCTSOFTWARE-style failure this guards against)."""
                if src_table in confirmed_mappings:
                    return confirmed_mappings[src_table], "confirmed"
                exact = upper_sf.get(src_table.upper())
                if exact:
                    return exact, "exact"
                if not sf_tables_live:
                    # Live Snowflake table discovery failed entirely — nothing
                    # to compare against, fall back to a plain guess (the
                    # manual-entry dropdown still lets a human override it).
                    return src_table.upper(), "no_data"
                close = difflib.get_close_matches(src_table.upper(), list(upper_sf.keys()), n=3, cutoff=0.4)
                if len(close) >= 2:
                    return "", "ambiguous"
                if len(close) == 1:
                    return upper_sf[close[0]], "fuzzy"
                return "", "not_found"

            suggestions = {t: suggest(t) for t in source_tables}
            ambiguous_tables = {t for t, (_, status) in suggestions.items() if status == "ambiguous"}
            not_found_tables = {t for t, (_, status) in suggestions.items() if status == "not_found"}
            select_options = sorted(set(sf_tables_live) | {s for s, _ in suggestions.values() if s})

            _STATUS_LABELS = {
                "confirmed": "✓ Previously confirmed",
                "exact": "",
                "fuzzy": "",
                "ambiguous": "⚠️ Ambiguous — pick manually",
                "not_found": "⚠️ No close match found — pick manually",
                "no_data": "⚠️ Could not verify (Snowflake table list unavailable)",
            }

            def status_for(src_table: str) -> str:
                return _STATUS_LABELS[suggestions[src_table][1]]

            import pandas as pd
            mapping_df = pd.DataFrame({
                "Source Table": source_tables,
                "Snowflake Target Table": [suggestions[t][0] for t in source_tables],
                "Status": [status_for(t) for t in source_tables],
            })

            edited_df = st.data_editor(
                mapping_df,
                column_config={
                    "Source Table": st.column_config.TextColumn(disabled=True),
                    "Snowflake Target Table": st.column_config.SelectboxColumn(
                        options=select_options, required=True,
                        help="Auto-suggested via exact/fuzzy match against live Snowflake tables — override if wrong.",
                    ),
                    "Status": st.column_config.TextColumn(disabled=True),
                },
                hide_index=True,
                width='stretch',
                key="batch_mapping_editor",
            )
            target_map = dict(zip(edited_df["Source Table"], edited_df["Snowflake Target Table"]))

            # ── Quality checks on the mapping before allowing Generate ──────
            empty_targets = [s for s, t in target_map.items() if not t]
            target_counts: dict = {}
            for t in target_map.values():
                if t:
                    target_counts[t] = target_counts.get(t, 0) + 1
            duplicate_targets = [t for t, n in target_counts.items() if n > 1]

            if ambiguous_tables:
                st.warning(
                    f"Ambiguous target for: {', '.join(sorted(ambiguous_tables))} — "
                    f"multiple Snowflake tables matched closely, pick the correct one in the grid above."
                )
            if not_found_tables:
                st.warning(
                    f"No confident Snowflake match for: {', '.join(sorted(not_found_tables))} — "
                    f"pick the correct target manually in the grid above (nothing was auto-filled to avoid guessing wrong)."
                )
            if empty_targets:
                st.error(f"Missing target table for: {', '.join(empty_targets)}")
            if duplicate_targets:
                st.error(
                    f"Two or more source tables are mapped to the same Snowflake target "
                    f"({', '.join(duplicate_targets)}) — each source table needs a distinct target."
                )
            mapping_valid = source_tables and not empty_targets and not duplicate_targets
            if mapping_valid:
                st.success(f"{len(source_tables)} source table(s) mapped to {len(source_tables)} distinct target(s) — ready to generate.")

        st.markdown("### 4. Set column exclusions")
        st.caption("System exclusions apply automatically. Add table-specific exclusions only when needed.")
        auto_excluded = _get_all_exclusions(src_db_type)
        static_set = {c.lower() for c in STATIC_EXCLUDE_COLUMNS}
        user_global_excluded = [c for c in auto_excluded if c not in static_set]
        st.caption(
            f"🔒 Built-in auto-excluded (system default for {_DB_TYPE_LABELS.get(src_db_type, src_db_type)}, always "
            f"applied to every table below, not shown in the pickers): {', '.join(STATIC_EXCLUDE_COLUMNS) or '(none)'}"
        )
        st.caption(
            f"🌐 User-defined global exclusions (added via the Exclusions tab, always applied to every table below, "
            f"not shown in the pickers — manage the list in the Exclusions tab): {', '.join(user_global_excluded) or '(none)'}"
        )

        per_table_excl: dict = {}
        for src_table in source_tables:
            with st.expander(f"Columns to exclude — {src_table}", expanded=False):
                try:
                    table_cols = cached_source_columns(
                        src_db_type, rec["host"], int(rec.get("port") or 0), database,
                        rec["username"], source_password(rec), rec.get("auth", ""),
                        rec.get("s3_output", ""), schema, src_table,
                    )
                except Exception as exc:
                    st.warning(f"Could not load columns for {src_table}: {exc}")
                    table_cols = []
                static_present = sorted(c for c in table_cols if c.lower() in static_set)
                user_global_present = sorted(c for c in table_cols if c.lower() in set(auto_excluded) and c.lower() not in static_set)
                pickable_cols = [c for c in table_cols if c.lower() not in set(auto_excluded)]
                if static_present:
                    st.caption(f"🔒 Already built-in auto-excluded: {', '.join(static_present)}")
                if user_global_present:
                    st.caption(f"🌐 Already user-defined auto-excluded: {', '.join(user_global_present)}")
                per_table_excl[src_table] = st.multiselect(
                    f"Additional columns to exclude from {src_table} (optional, just for this run)",
                    options=pickable_cols,
                    default=[],
                    key=f"batch_excl_{src_table}",
                    label_visibility="collapsed",
                )

        model = select_or_type(
            "AI model", available_models_for_ui(), os.getenv("DIAL_MODEL", "gpt-4o"),
            "batch_model", format_func=_model_label,
        )

        layer, output_dir = pick_layer("batch_layer")

        per_table_col_overrides: dict = {}
        if source_tables and mapping_valid:
            st.markdown("### 5. Review columns for each table")
            st.caption("Expand each table. Confirm matches before continuing.")
            batch_extractor = ExtractorFactory.create(
                src_db_type, host=rec["host"], port=int(rec.get("port") or 0),
                database=database, username=rec["username"], password=source_password(rec),
                auth=rec.get("auth", ""), s3_output=rec.get("s3_output", ""),
            )
            preview_pipeline = ValidationPipeline(model=model, source_extractor=batch_extractor)
            for src_table in source_tables:
                tgt_table = target_map.get(src_table, "")
                if not tgt_table:
                    continue
                with st.expander(f"Column mapping — {src_table} → {tgt_table}", expanded=False):
                    per_table_col_overrides[src_table] = render_mapping_review(
                        preview_pipeline, schema, src_table, sf_schema, tgt_table,
                        sf_database, database,
                        (list(auto_excluded) + per_table_excl.get(src_table, [])),
                        key_prefix=f"batch_{src_table}",
                    )
                    batch_mapping_rows = st.session_state.get(f"batch_{src_table}_mapping_rows")
                    if batch_mapping_rows:
                        render_custom_sql_section(
                            batch_mapping_rows, src_db_type,
                            src_table_fqn=f"{schema}.{src_table}",
                            sf_table_fqn=(
                                f"{sf_database}.{sf_schema}.{tgt_table}" if sf_database
                                else f"{sf_schema}.{tgt_table}"
                            ),
                            output_dir=output_dir,
                            default_filename=src_table,
                            key_prefix=f"batch_{src_table}",
                        )

        generate_disabled = not source_tables or not target_map or any(not t for t in target_map.values()) or (
            len(set(target_map.values())) != len(target_map)
        )

        # ── Pre-generate approval check (batch) ─────────────────────────────────
        # Aggregate low-confidence and unmatched columns across ALL tables that
        # have been previewed, and surface them before the Generate All button.
        _batch_issues: list = []
        for _bt in (source_tables or []):
            _bt_rows = st.session_state.get(f"batch_{_bt}_mapping_rows") or []
            _bt_corrected = set((per_table_col_overrides.get(_bt) or {}).keys())
            _bt_skipped_low = [
                r for r in _bt_rows
                if r.get("skip_validation") and not r.get("target_column")
                and r["source_column"] not in _bt_corrected
                and r.get("confidence", 0.0) < 0.75
            ]
            _bt_low = [
                r for r in _bt_rows
                if not r.get("skip_validation") and r.get("target_column")
                and r["source_column"] not in _bt_corrected
                and r.get("confidence", 1.0) < 0.75
            ]
            _bt_none = [r for r in _bt_rows if not r.get("skip_validation") and not r.get("target_column")]
            if _bt_skipped_low or _bt_low or _bt_none:
                _batch_issues.append({"table": _bt, "skipped_low": _bt_skipped_low, "low": _bt_low, "none": _bt_none})

        _batch_generate_blocked = False
        if _batch_issues and not generate_disabled:
            with st.container(border=True):
                st.markdown("##### ⚠️ Review required before generating")
                for _bi in _batch_issues:
                    st.markdown(f"**{_bi['table']}**")
                    if _bi.get("skipped_low"):
                        _bsk_c1, _bsk_c2 = st.columns([3, 1])
                        with _bsk_c1:
                            st.warning(
                                f"{len(_bi['skipped_low'])} column(s) skipped — no target match (confidence <75%): "
                                + ", ".join(
                                    f"`{r['source_column']}` ({int(r.get('confidence',0)*100)}%)"
                                    for r in _bi["skipped_low"]
                                )
                            )
                        with _bsk_c2:
                            if st.button("🎫 Raise Jira ticket", key=f"batch_skip_jira_{_bi['table']}"):
                                try:
                                    from connector.jira_client import create_ticket, is_configured
                                    if not is_configured():
                                        st.info("Jira not configured — set env vars in `.env`.")
                                    else:
                                        _t = create_ticket(
                                            summary=f"[Migration Validator] Skipped columns: {_bi['table']}",
                                            description=(
                                                f"Table: {_bi['table']}\n\nSkipped columns (no target, conf <75%):\n"
                                                + "\n".join(
                                                    f"  - {r['source_column']} ({int(r.get('confidence',0)*100)}%,"
                                                    f" {r.get('skip_reason','no match')})"
                                                    for r in _bi["skipped_low"]
                                                )
                                            ),
                                            labels=["migration-validator", "skipped-columns", "needs-review"],
                                        )
                                        st.success(f"[{_t['key']}]({_t['url']})")
                                except Exception as _bsje:
                                    st.error(f"Jira error: {_bsje}")
                    if _bi["none"]:
                        st.error(
                            f"No target match for: "
                            f"`{'`, `'.join(r['source_column'] for r in _bi['none'])}` — will be skipped."
                        )
                    if _bi["low"]:
                        st.warning(
                            "Low-confidence mappings (<75%): "
                            + ", ".join(
                                f"`{r['source_column']}` → `{r['target_column']}` ({int(r['confidence']*100)}%)"
                                for r in _bi["low"]
                            )
                        )
                _bj_col1, _bj_col2 = st.columns([3, 1])
                with _bj_col2:
                    if st.button("🎫 Raise Jira tickets", key="batch_jira_btn"):
                        try:
                            from connector.jira_client import create_ticket, is_configured
                            if not is_configured():
                                st.info("Jira not configured — set `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY` in your `.env`.")
                            else:
                                _created = []
                                for _bi in _batch_issues:
                                    if not _bi["low"] and not _bi["none"]:
                                        continue
                                    _bdesc = (
                                        f"Table: {_bi['table']}\n\n"
                                        + (f"Unmatched: {', '.join(r['source_column'] for r in _bi['none'])}\n" if _bi["none"] else "")
                                        + (f"Low-confidence:\n" + "\n".join(f"  - {r['source_column']} → {r['target_column']} ({int(r['confidence']*100)}%)" for r in _bi["low"]) if _bi["low"] else "")
                                    )
                                    _t = create_ticket(
                                        summary=f"[Migration Validator] Low-confidence mappings: {_bi['table']}",
                                        description=_bdesc,
                                        labels=["migration-validator", "needs-review"],
                                    )
                                    _created.append(f"[{_t['key']}]({_t['url']})")
                                st.success(f"Created {len(_created)} ticket(s): {', '.join(_created)}")
                        except Exception as _bje:
                            st.error(f"Jira error: {_bje}")
                _batch_confirmed = st.checkbox(
                    "I have reviewed the issues above and want to generate anyway (or have raised Jira tickets)",
                    key="batch_review_confirmed",
                )
                if not _batch_confirmed:
                    _batch_generate_blocked = True

        # ── Per-table migration filters + optional JOIN rules ──────────────
        # Each table gets its own filter expander so different tables can have
        # different predicates (e.g. orders: created_at >= '2024-01-01',
        # customers: is_active = true).
        # Previously-used filters for each table are surfaced from plan history
        # as a dropdown — no need to retype the same predicate every run.
        # Each expander also lets you add LEFT JOINs for that table; the join
        # spec and a natural-language prompt are forwarded to AISQLQueryGenerator
        # instead of the default column-mapping pipeline.
        st.markdown("### 6. Define validation scope per table")
        st.caption(
            "**What is this?**  When the migration team moved only a *subset* of rows from a table "
            "(e.g. only the last 2 years of orders, or only active customers), the validator must "
            "apply the same filter on the source side — otherwise it will always report a mismatch "
            "because it is comparing the full source against the partial target.\n\n"
            "**Source filter** is applied to the source database query. "
            "**Target filter** is applied to the Snowflake query (defaults to the source filter when left blank). "
            "Write the predicate *without* the WHERE keyword — e.g. `created_at >= '2024-01-01'` or "
            "`status = 'active' AND tenant_id = 42`.\n\n"
            "**Join rules (optional)** — if this table needs to be validated via a JOIN query (e.g. "
            "orders → customers), configure the LEFT JOINs and provide a short prompt. "
            "AI generates the SQL using the live schema. "
            "Previously-used filters for each table are shown in a dropdown so you can reuse them without retyping."
        )

        per_table_filters: dict = {}   # {src_table: (source_filter, target_filter)}
        per_table_joins:   dict = {}   # {src_table: {"joins": [...], "prompt": str, "join_sf_schema": str}}
        _pt_all_table_options = [f"{schema}.{t}" for t in source_tables]
        for src_table in source_tables:
            _history = filter_options_for(src_table)
            _label = f"🔍 Migration filter — {src_table}"
            _has_history = bool(_history)
            with st.expander(_label + (" *(history available)*" if _has_history else ""), expanded=False):
                if _has_history:
                    _dropdown_labels = [f"{sf}  →  {tf or '(same as source)'}" for sf, tf in _history]
                    _dropdown_labels = ["— Enter a new filter —"] + _dropdown_labels
                    _selected_idx = st.selectbox(
                        "Previously used filters for this table",
                        options=range(len(_dropdown_labels)),
                        format_func=lambda i: _dropdown_labels[i],
                        key=f"batch_filter_history_{src_table}",
                        help="Filters are saved automatically from each successful generation run. "
                             "Select one to pre-fill the fields below, or choose 'Enter a new filter' to type manually.",
                    )
                    _pre_src = _history[_selected_idx - 1][0] if _selected_idx > 0 else ""
                    _pre_tgt = _history[_selected_idx - 1][1] if _selected_idx > 0 else ""
                else:
                    _pre_src, _pre_tgt = "", ""

                _fc1, _fc2 = st.columns(2)
                with _fc1:
                    _src_f = st.text_input(
                        "Source filter",
                        value=_pre_src,
                        placeholder="e.g.  created_at >= '2024-01-01'",
                        key=f"batch_src_filter_{src_table}",
                        help="WHERE predicate applied to the source database query for this table. "
                             "No WHERE keyword — just the condition, e.g. `status = 'active'`.",
                    )
                with _fc2:
                    _tgt_f = st.text_input(
                        "Target filter",
                        value=_pre_tgt,
                        placeholder="Leave blank to mirror source filter",
                        key=f"batch_tgt_filter_{src_table}",
                        help="WHERE predicate applied to the Snowflake query. "
                             "Column names are usually UPPER_CASE on Snowflake. "
                             "If blank, the source filter is reused as-is.",
                    )

                if _src_f:
                    st.info(
                        f"**{src_table}** source: `WHERE {_src_f}`  \n"
                        f"**{src_table}** target: `WHERE {_tgt_f or _src_f}`"
                    )
                else:
                    st.caption("No filter — full table scan (both sides).")

                per_table_filters[src_table] = (_src_f, _tgt_f)

                # ── Optional per-table JOIN rules ──────────────────────────────
                st.divider()
                _pt_use_joins = st.checkbox(
                    "Add JOIN rules for this table (optional)",
                    key=f"pt_use_joins_{src_table}",
                    help="When this table must be validated via a JOIN query, enable this to define "
                         "LEFT JOIN conditions. AI generates the SQL using live schema context.",
                )
                if _pt_use_joins:
                    st.caption(
                        "Define LEFT JOINs below. This table is the driving (left) side. "
                        "Select the joined table and write the ON condition without the ON keyword."
                    )
                    _pt_join_count = st.number_input(
                        "Number of LEFT JOINs",
                        min_value=1, max_value=max(1, len(_pt_all_table_options) - 1),
                        value=1, step=1,
                        key=f"pt_join_count_{src_table}",
                    )
                    _pt_join_rules: list = []
                    for _pt_ji in range(int(_pt_join_count)):
                        _ptj1, _ptj2 = st.columns(2)
                        with _ptj1:
                            _pt_right = st.selectbox(
                                f"LEFT JOIN {_pt_ji + 1} — table",
                                options=_pt_all_table_options,
                                key=f"pt_join_right_{src_table}_{_pt_ji}",
                            )
                        with _ptj2:
                            _pt_on = st.text_input(
                                f"LEFT JOIN {_pt_ji + 1} — ON condition",
                                placeholder=f"{src_table}.id = {_pt_right.split('.')[-1] if _pt_all_table_options else 'other'}.{src_table}_id",
                                key=f"pt_join_on_{src_table}_{_pt_ji}",
                                help="Condition without ON keyword. Use table.column notation.",
                            )
                        if _pt_on.strip():
                            _pt_join_rules.append({"right": _pt_right, "on": _pt_on.strip()})

                    _pt_join_prompt = st.text_area(
                        "Prompt for AI SQL generation",
                        placeholder=f"Validate {src_table} joined to products. "
                                    "Compare row counts and key aggregates after applying the filter above.",
                        key=f"pt_join_prompt_{src_table}",
                        height=80,
                        help="Describe what the joined query should validate. "
                             "AI uses live PK/FK schema for fully-qualified SQL.",
                    ).strip()

                    _pt_join_sf_schema_input = st.text_input(
                        "Snowflake target schema for joined tables (leave blank to use selected target schema)",
                        key=f"pt_join_sf_schema_{src_table}",
                        placeholder=f"e.g. {sf_schema}",
                    ).strip() or sf_schema

                    if int(_pt_join_count) > len(_pt_join_rules):
                        st.warning("Fill in every ON condition before generating.")

                    if _pt_join_rules and _pt_join_prompt:
                        per_table_joins[src_table] = {
                            "joins": _pt_join_rules,
                            "prompt": _pt_join_prompt,
                            "join_sf_schema": _pt_join_sf_schema_input,
                        }
                        st.info(
                            f"**{src_table}** will be validated with {len(_pt_join_rules)} LEFT JOIN(s). "
                            f"AI prompt: _{_pt_join_prompt[:80]}{'…' if len(_pt_join_prompt) > 80 else ''}_"
                        )

        st.divider()
        if per_table_joins:
            st.info(
                f"**{len(per_table_joins)} table(s)** have JOIN rules configured and will use "
                "AI SQL generation instead of the standard column-mapping pipeline."
            )
        st.caption("Final check: every source table has one unique target, mappings are reviewed, and scope is set.")
        if st.button("Generate all table YAMLs", type="primary", key="batch_generate", disabled=generate_disabled or _batch_generate_blocked):
            extractor = ExtractorFactory.create(
                src_db_type, host=rec["host"], port=int(rec.get("port") or 0),
                database=database, username=rec["username"], password=source_password(rec),
                auth=rec.get("auth", ""), s3_output=rec.get("s3_output", ""),
            )
            progress = st.progress(0.0, text="Starting...")
            results = []
            pairs = list(target_map.items())
            # Build AI generator once — reused by all tables that have JOIN rules
            _pt_gen = AISQLQueryGenerator(model=model) if per_table_joins else None
            _pt_sf_creds = snowflake_creds()

            for i, (src_table, tgt_table) in enumerate(pairs, 1):
                progress.progress(i / len(pairs), text=f"{src_table} → {tgt_table}  ({i}/{len(pairs)})")
                try:
                    _pt_jinfo = per_table_joins.get(src_table)
                    if _pt_jinfo:
                        # ── JOIN path: use AISQLQueryGenerator ──────────────
                        from excel_batch_loader import _build_schema_context as _pt_bsc
                        _pt_join_rules = _pt_jinfo["joins"]
                        _pt_join_prompt = _pt_jinfo["prompt"]
                        _pt_sf_sch_override = _pt_jinfo.get("join_sf_schema") or sf_schema
                        _pt_src_filter, _pt_tgt_filter = per_table_filters.get(src_table, ("", ""))

                        _pt_rule_lines = [f"Driving table: {schema}.{src_table}"]
                        _pt_rule_lines.extend(
                            f"LEFT JOIN {r['right']} ON {r['on']}" for r in _pt_join_rules
                        )
                        _pt_join_spec = (
                            "\n\nSTRUCTURED VALIDATION RULES (mandatory):\n"
                            + "\n".join(_pt_rule_lines)
                            + f"\nSource WHERE predicate: {_pt_src_filter or '(none)'}"
                            + f"\nSnowflake WHERE predicate: {_pt_tgt_filter or _pt_src_filter or '(none)'}"
                            + "\nUse LEFT JOIN only. Do not use INNER JOIN, RIGHT JOIN, FULL JOIN, CROSS JOIN, or comma joins."
                            + " Apply source predicate only in source SQL and Snowflake predicate only in Snowflake SQL."
                        )

                        _pt_src_ctx = _pt_bsc(
                            extractor, src_db_type, database, schema,
                            [src_table] + [r["right"].split(".")[-1] for r in _pt_join_rules],
                            grain_cols=[],
                        )
                        _pt_tgt_ctx: dict = {}
                        if _pt_sf_creds.get("account") and sf_database:
                            _pt_sf_ext = SnowflakeExtractor(
                                account=_pt_sf_creds["account"],
                                database=sf_database, schema=_pt_sf_sch_override,
                                username=_pt_sf_creds["username"],
                                password=_pt_sf_creds["password"],
                            )
                            _pt_tgt_ctx = _pt_bsc(
                                _pt_sf_ext, "snowflake", sf_database, _pt_sf_sch_override,
                                [tgt_table.upper()] + [r["right"].split(".")[-1].upper() for r in _pt_join_rules],
                                grain_cols=[],
                            )

                        _pt_src_sql = _pt_gen.generate_schema_aware_query(
                            user_instruction=_pt_join_prompt + _pt_join_spec,
                            schema_context=_pt_src_ctx,
                            db_type=src_db_type,
                            default_schema=schema,
                            normalize=True,
                        ).query
                        _pt_tgt_sql = _pt_gen.generate_schema_aware_query(
                            user_instruction=_pt_join_prompt + _pt_join_spec,
                            schema_context=_pt_tgt_ctx,
                            db_type="snowflake",
                            default_schema=_pt_sf_sch_override,
                            normalize=True,
                        ).query

                        import yaml as _pt_yaml
                        _pt_out_dir = Path(output_dir) / "data_validation"
                        _pt_out_dir.mkdir(parents=True, exist_ok=True)
                        _pt_path = _pt_out_dir / f"{src_table}.yaml"
                        _pt_sql_oneline = lambda s: " ".join(s.split())
                        _pt_doc = {
                            "tables": {
                                src_table: {
                                    "validations": {
                                        "data_validation": {
                                            "source_table_name": src_table,
                                            "source": src_db_type,
                                            "source_database": database,
                                            "source_schema": schema,
                                            "pksourcecolumn": "row_hash",
                                            "sourcequery": _pt_sql_oneline(_pt_src_sql),
                                            "target_table_name": tgt_table,
                                            "target": "snowflake",
                                            "target_database": sf_database,
                                            "target_schema": _pt_sf_sch_override,
                                            "pktargetcolumn": "row_hash",
                                            "targetquery": _pt_sql_oneline(_pt_tgt_sql),
                                            "source_filter": _pt_src_filter,
                                            "target_filter": _pt_tgt_filter or _pt_src_filter,
                                            "joins": _pt_join_rules,
                                        }
                                    }
                                }
                            }
                        }
                        with open(_pt_path, "w", encoding="utf-8") as _pt_f:
                            _pt_yaml.dump(_pt_doc, _pt_f, allow_unicode=True,
                                          sort_keys=False, default_flow_style=False)
                        results.append({
                            "Source": src_table, "Target": tgt_table, "Status": "✅ Success (JOIN)",
                            "Detail": f"AI JOIN SQL, {len(_pt_join_rules)} join(s)",
                        })
                    else:
                        # ── Standard column-mapping pipeline ─────────────────
                        pipeline = ValidationPipeline(model=model, source_extractor=extractor)
                        result, _plan = pipeline.run_with_plan(
                            pg_schema=schema,
                            pg_table=src_table,
                            sf_schema=sf_schema,
                            sf_table=tgt_table,
                            sf_database=sf_database,
                            pg_database=database,
                            explicit_mappings=per_table_col_overrides.get(src_table) or None,
                            exclude_columns=(list(auto_excluded) + per_table_excl.get(src_table, [])) or None,
                            source_db_type=src_db_type,
                            output_dir=output_dir,
                            source_filter=per_table_filters.get(src_table, ("", ""))[0],
                            target_filter=per_table_filters.get(src_table, ("", ""))[1],
                        )
                        results.append({
                            "Source": src_table, "Target": tgt_table, "Status": "✅ Success",
                            "Detail": f"{result.active_columns} cols, {result.generated_by}",
                        })
                        creds = snowflake_creds()
                        if creds["account"]:
                            mapping_store.save_mapping(
                                creds["account"], creds["username"], creds["password"],
                                sf_database, sf_schema, src_table, tgt_table,
                                confirmed_by=creds["username"], source_connection=connection_label(rec),
                            )
                except Exception as exc:
                    results.append({"Source": src_table, "Target": tgt_table, "Status": "❌ Failed", "Detail": str(exc)})
            progress.empty()

            st.dataframe(results, width='stretch', hide_index=True)
            n_ok = sum(1 for r in results if r["Status"].startswith("✅"))
            if n_ok == len(results):
                st.success(f"Batch complete: {n_ok}/{len(results)} table(s) generated successfully.")
            else:
                st.warning(f"Batch complete: {n_ok}/{len(results)} table(s) generated successfully — see failures above.")

    elif rec and _batch_mode == "📊 Report Pack (Excel)":
        # =====================================================================
        # REPORT PACK MODE — Excel mapping sheet input; each row is
        # run through derive_row_plan() first (tables/grain/filter, AI-derived)
        # so the user reviews/edits the plan *before* any YAML is generated.
        # Single-table rows generate via run_with_plan() (base/learned rules +
        # AI only for ambiguous columns); join rows keep the existing
        # write_yaml()/_generate_queries() path unchanged — no new yaml.dump().
        # =====================================================================
        import hashlib as _aip_hashlib
        import pandas as _aip_pd
        import tempfile as _aip_tempfile
        from excel_batch_loader import load_excel as _aip_load_excel, derive_row_plan as _aip_derive_row_plan
        from excel_batch_loader import _generate_queries as _aip_generate_queries, write_yaml as _aip_write_yaml

        _override_source_env(rec)
        src_db_type = rec["db_type"]

        with st.container(border=True):
            st.markdown("**① Source location** (same connection selected above)")
            _aip_database, _aip_schema, _ = pick_source_location(rec, "aip")

        with st.container(border=True):
            st.markdown("**② Target (Snowflake)**")
            _aip_sf_database, _aip_sf_schema, _ = pick_snowflake_target("", "aip", include_table=False)

        st.markdown("**③ Upload mapping sheet**")
        st.caption(
            "Expected columns: Report Pack · Yaml-File-name · Report Name · Summary · Grain · "
            "Legacy Query (Redshift/Postgres/…) · Snowflake Query. Headers that don't match the "
            "expected names (e.g. 'Description' instead of 'Summary') are classified by AI on "
            "upload; anything AI can't place is surfaced as an unrecognized column below."
        )

        _aip_c1, _aip_c2, _aip_c3 = st.columns([2, 2, 3])
        with _aip_c1:
            _aip_env = st.text_input("Environment", placeholder="dev / prod / uat …", key="aip_env",
                                      help="Replaces {env} tokens in SQL. Leave blank to keep as placeholder.")
        with _aip_c2:
            _aip_sheet = st.text_input("Sheet name (optional)", placeholder="First sheet if blank", key="aip_sheet")
        with _aip_c3:
            _aip_model = select_or_type(
                "AI model", available_models_for_ui(), os.getenv("DIAL_MODEL", "gpt-4o"),
                "aip_model", format_func=_model_label,
            )
        _aip_dry = st.checkbox("Dry run — preview only, no files written", key="aip_dry")

        _aip_file = st.file_uploader("Upload Excel mapping sheet (.xlsx)", type=["xlsx", "xls"], key="aip_excel_upload")

        if _aip_file and _aip_database and _aip_schema:
            _aip_bytes = _aip_file.read()
            # Stable per-upload cache key derived from file content (not a
            # freshly-generated temp path, which changes every rerun and would
            # defeat the plan/extractor caching below).
            _aip_file_key = _aip_hashlib.md5(_aip_bytes).hexdigest()

            _aip_tmp_path_key = f"aip_tmp_path_{_aip_file_key}"
            if _aip_tmp_path_key not in st.session_state:
                with _aip_tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as _aip_tmp:
                    _aip_tmp.write(_aip_bytes)
                    st.session_state[_aip_tmp_path_key] = _aip_tmp.name
            _aip_tmp_path = st.session_state[_aip_tmp_path_key]

            _aip_unrecognized: list = []
            try:
                _aip_gen = AISQLQueryGenerator(model=_aip_model)
                _aip_specs = _aip_load_excel(
                    _aip_tmp_path, sheet=_aip_sheet.strip() or None,
                    unrecognized_out=_aip_unrecognized, ai_generator=_aip_gen,
                )
            except Exception as _aip_exc:
                st.error(f"Could not parse sheet: {_aip_exc}")
                _aip_specs = []
                _aip_gen = None

            if _aip_unrecognized:
                st.warning(
                    f"Unrecognized column(s) — neither regex nor AI could place them, ignored: "
                    f"{', '.join(_aip_unrecognized)}"
                )

            if _aip_specs:
                # Populate connection context on every spec (same as Report Pack)
                # and derive each row's plan once per upload, cached in session
                # state keyed by a hash of the uploaded file's bytes (stable
                # across reruns) so grid edits/checkbox toggles don't re-trigger
                # a fresh AI call or a fresh source DB connection.
                for _s in _aip_specs:
                    _s.source_database = _aip_database
                    _s.source_schema = _aip_schema
                    _s.source_tables = [_s.yaml_file_name]
                    _s.sf_database = _aip_sf_database or ""
                    _s.sf_schema = _aip_sf_schema or ""

                _aip_extractor_key = f"aip_extractor_{_aip_file_key}"
                if _aip_extractor_key not in st.session_state:
                    st.session_state[_aip_extractor_key] = ExtractorFactory.create(
                        src_db_type, host=rec["host"], port=int(rec.get("port") or 0),
                        database=_aip_database, username=rec["username"],
                        password=source_password(rec),
                        auth=rec.get("auth", ""), s3_output=rec.get("s3_output", ""),
                    )
                _aip_extractor = st.session_state[_aip_extractor_key]

                _aip_plans_key = f"aip_plans_{_aip_file_key}"
                if _aip_plans_key not in st.session_state:
                    with st.spinner("Deriving AI plan for each row…"):
                        _aip_plans = {}
                        for _s in _aip_specs:
                            try:
                                _aip_plans[_s.row_num] = _aip_derive_row_plan(
                                    _s, source_extractor=_aip_extractor,
                                    ai_generator=_aip_gen, model=_aip_model,
                                )
                            except Exception as _aip_plan_exc:
                                _aip_plans[_s.row_num] = {
                                    "tables": _s.source_tables, "grain_columns": [],
                                    "filter_english": "", "filter_sql": "",
                                    "join_needed": len(_s.source_tables) > 1,
                                    "warnings": [f"Plan derivation failed: {_aip_plan_exc}"],
                                }
                        st.session_state[_aip_plans_key] = _aip_plans
                _aip_plans = st.session_state[_aip_plans_key]

                st.success(f"Parsed **{len(_aip_specs)}** row(s). Review the AI-derived plan below, then confirm.")

                _aip_grid_rows = []
                for _s in _aip_specs:
                    _p = _aip_plans.get(_s.row_num, {})
                    _aip_grid_rows.append({
                        "Row": _s.row_num,
                        "Report Pack": _s.report_pack,
                        "YAML File": _s.yaml_file_name,
                        "Tables (AI)": ", ".join(_p.get("tables", [])),
                        "Grain (AI)": ", ".join(_p.get("grain_columns", [])),
                        "Filter (AI)": _p.get("filter_english", ""),
                        "Status": "🔗 Join" if _p.get("join_needed") else "📄 Single table",
                    })
                _aip_edited = st.data_editor(
                    _aip_pd.DataFrame(_aip_grid_rows),
                    column_config={
                        "Row": st.column_config.NumberColumn(disabled=True),
                        "Report Pack": st.column_config.TextColumn(disabled=True),
                        "YAML File": st.column_config.TextColumn(disabled=True),
                        "Tables (AI)": st.column_config.TextColumn(disabled=True),
                        "Grain (AI)": st.column_config.TextColumn(disabled=True),
                        "Filter (AI)": st.column_config.TextColumn(
                            help="Editable plain-English filter — edit here or in the row drill-down below, "
                                 "then re-derive to update the SQL predicate. NOTE: for 🔗 Join rows this is "
                                 "preview-only and is NOT applied to the generated SQL (see drill-down).",
                        ),
                        "Status": st.column_config.TextColumn(disabled=True),
                    },
                    hide_index=True,
                    width='stretch',
                    key="aip_batch_grid",
                )
                st.caption(
                    "⚠️ **Filter (AI)** is preview-only for 🔗 Join rows — the join-generation path "
                    "(`_generate_queries()`) does not read this field, so edits here have no effect on the "
                    "generated SQL for those rows. It only takes effect for 📄 Single table rows."
                )
                # Push any grid edits to the plain-English filter back into each plan
                for _row in _aip_edited.to_dict("records"):
                    _p = _aip_plans.get(_row["Row"])
                    if _p is not None:
                        _p["filter_english"] = _row["Filter (AI)"]

                st.markdown("#### Row drill-down")
                _aip_col_overrides: dict = {}
                _aip_review_pipeline = ValidationPipeline(model=_aip_model, source_extractor=_aip_extractor)
                for _s in _aip_specs:
                    _p = _aip_plans.get(_s.row_num, {})
                    for _w in _p.get("warnings", []):
                        st.warning(f"Row {_s.row_num} ({_s.yaml_file_name}): {_w}")
                    with st.expander(f"Row {_s.row_num} — {_s.yaml_file_name}  ({'JOIN' if _p.get('join_needed') else 'single table'})", expanded=False):
                        if _p.get("join_needed"):
                            st.warning(
                                "This filter is preview-only for join rows — it is NOT applied to the "
                                "generated SQL. Join SQL comes entirely from the existing schema-aware "
                                "generator (`_generate_queries()`), which does not read this description."
                            )
                        _aip_desc = st.text_area(
                            "Plan description (plain English)",
                            value=_p.get("filter_english", ""),
                            key=f"aip_desc_{_s.row_num}",
                            height=80,
                            disabled=_p.get("join_needed", False),
                        )
                        if st.button("🔄 Re-derive from this description", key=f"aip_rederive_{_s.row_num}"):
                            _s.filter_condition = _aip_desc
                            with st.spinner("Re-deriving…"):
                                try:
                                    _aip_plans[_s.row_num] = _aip_derive_row_plan(
                                        _s, source_extractor=_aip_extractor,
                                        ai_generator=_aip_gen, model=_aip_model,
                                    )
                                    st.session_state[_aip_plans_key] = _aip_plans
                                    st.rerun()
                                except Exception as _aip_rd_exc:
                                    st.error(f"Re-derive failed: {_aip_rd_exc}")

                        if _p.get("join_needed"):
                            st.caption(
                                "Multi-table row — full JOIN SQL is generated at YAML-write time via the "
                                "existing schema-aware generator (unchanged). Column-mapping review does "
                                "not apply to a raw multi-table query."
                            )
                            st.code(
                                f"Tables: {', '.join(_p.get('tables', []))}\n"
                                f"Grain: {', '.join(_p.get('grain_columns', []))}\n"
                                f"Filter: {_p.get('filter_sql') or '(none)'}\n"
                                f"Summary: {_s.summary}",
                                language="sql",
                            )
                        else:
                            _aip_col_overrides[_s.row_num] = render_mapping_review(
                                _aip_review_pipeline, _aip_schema, _s.yaml_file_name,
                                _aip_sf_schema, _s.yaml_file_name,
                                _aip_sf_database, _aip_database,
                                [], key_prefix=f"aip_{_s.row_num}",
                            )

                _aip_confirmed = st.checkbox(
                    "✅ I have reviewed the AI-derived plan above and confirm generating YAMLs",
                    key="aip_confirmed",
                )

                if st.button("▶️ Generate Report YAMLs", type="primary", key="aip_generate",
                             disabled=not _aip_confirmed):
                    _aip_env_val = _aip_env.strip() or None
                    _aip_out_dir = _ROOT_DIR / "Project" / "config" / "report"
                    _aip_sf_creds = snowflake_creds()
                    _aip_sf_extractor = SnowflakeExtractor(
                        account=_aip_sf_creds["account"],
                        database=_aip_sf_database,
                        schema=_aip_sf_schema,
                        username=_aip_sf_creds["username"],
                        password=_aip_sf_creds["password"],
                    ) if _aip_sf_creds["account"] and _aip_sf_database else None

                    _aip_progress = st.progress(0.0, text="Starting…")
                    _aip_results: list = []
                    _aip_errors: list = []

                    for _aip_i, _s in enumerate(_aip_specs, 1):
                        _aip_progress.progress(
                            _aip_i / len(_aip_specs),
                            text=f"Processing {_s.yaml_file_name}  ({_aip_i}/{len(_aip_specs)})",
                        )
                        _p = _aip_plans.get(_s.row_num, {})
                        try:
                            if _p.get("join_needed"):
                                # ── Join rows: existing AI-SQL + write_yaml() path, unchanged ──
                                _s.filter_condition = _p.get("filter_sql") or _s.filter_condition
                                _aip_src_q, _aip_tgt_q = _aip_generate_queries(
                                    _s, _aip_model,
                                    source_extractor=_aip_extractor,
                                    sf_extractor=_aip_sf_extractor,
                                ) if (not _s.legacy_query or not _s.snowflake_query) \
                                  else (_s.legacy_query, _s.snowflake_query)
                                _aip_out = _aip_write_yaml(
                                    _s, _aip_src_q, _aip_tgt_q, _aip_env_val, _aip_out_dir,
                                    dry_run=_aip_dry,
                                )
                                _aip_results.append(str(_aip_out))
                            else:
                                # ── Single-table rows: run_with_plan() (base/learned rules) ──
                                _aip_pipeline = ValidationPipeline(model=_aip_model, source_extractor=_aip_extractor)
                                _aip_filter = _p.get("filter_sql") or ""
                                _aip_grain = _p.get("grain_columns") or []
                                _aip_result, _aip_plan = _aip_pipeline.run_with_plan(
                                    pg_schema=_aip_schema,
                                    pg_table=_s.yaml_file_name,
                                    sf_schema=_aip_sf_schema,
                                    sf_table=_s.yaml_file_name,
                                    sf_database=_aip_sf_database,
                                    pg_database=_aip_database,
                                    explicit_mappings=_aip_col_overrides.get(_s.row_num) or None,
                                    exclude_columns=None,
                                    source_db_type=src_db_type,
                                    output_dir=_aip_out_dir,
                                    source_filter=_aip_filter,
                                    target_filter=_aip_filter,
                                    candidate_keys=[_aip_grain] if _aip_grain else None,
                                )
                                _aip_results.append(str(_aip_result.yaml_path))
                        except Exception as _aip_row_exc:
                            _aip_errors.append(f"Row {_s.row_num} ({_s.yaml_file_name}): {_aip_row_exc}")

                    _aip_progress.progress(1.0, text="Done.")

                    if _aip_results:
                        st.success(f"✅ {'Would write' if _aip_dry else 'Written'} **{len(_aip_results)}** YAML file(s).")
                        with st.expander("Output files"):
                            for _aip_p in _aip_results:
                                st.code(_aip_p, language=None)
                    for _aip_e in _aip_errors:
                        st.error(_aip_e)
        elif _aip_file and not (_aip_database and _aip_schema):
            st.warning("Select a source database and schema above before uploading.")

# =============================================================================
# Row-hash SQL builder — used by Custom SQL tab when a table has no PK.
# Concatenates every normalized column with '|' separator and MD5-hashes the
# result.  Column order follows ordinal_position so both sides hash identically.
# ponytail: Phase 1 — pure hash, no dup_count.  Add GROUP BY + COUNT(*) when
#           duplicate-row tables are encountered and set-compare fails.

def _build_row_hash_sql(columns: list, table_fqn: str, db_type: str) -> str:
    """Return a SELECT MD5(<concat of all normalized cols>) AS row_hash FROM table_fqn."""
    sep = " || '|' || "
    parts = []
    for c in columns:
        col = c["column_name"]
        dt  = (c.get("data_type") or "text").lower()
        if db_type in ("postgresql", "postgres"):
            if "timestamp" in dt:
                expr = f"COALESCE(CAST(TO_CHAR({col}, 'YYYY-MM-DD HH24:MI:SS') AS TEXT), '<<NULL>>')"
            elif "date" == dt:
                expr = f"COALESCE(CAST(TO_CHAR({col}, 'YYYY-MM-DD') AS TEXT), '<<NULL>>')"
            elif "bool" in dt:
                expr = f"COALESCE(CAST(CASE WHEN {col} = true THEN '1' WHEN {col} = false THEN '0' ELSE NULL END AS TEXT), '<<NULL>>')"
            else:
                expr = f"COALESCE(CAST(TRIM({col}) AS TEXT), '<<NULL>>')"
        elif db_type in ("mssql", "sqlserver"):
            if "datetime" in dt or "date" == dt:
                expr = f"COALESCE(CAST(FORMAT({col}, 'yyyy-MM-dd HH:mm:ss') AS VARCHAR(MAX)), '<<NULL>>')"
            elif "bit" in dt:
                expr = f"COALESCE(CAST(CASE WHEN {col} = 1 THEN '1' ELSE '0' END AS VARCHAR(MAX)), '<<NULL>>')"
            else:
                expr = f"COALESCE(CAST(LTRIM(RTRIM({col})) AS VARCHAR(MAX)), '<<NULL>>')"
        elif db_type == "snowflake":
            if "timestamp" in dt or "date" == dt:
                expr = f"COALESCE(CAST(TO_VARCHAR({col}, 'YYYY-MM-DD HH24:MI:SS') AS STRING), '<<NULL>>')"
            elif "boolean" in dt:
                expr = f"COALESCE(CAST(CASE WHEN {col} = TRUE THEN '1' WHEN {col} = FALSE THEN '0' ELSE NULL END AS STRING), '<<NULL>>')"
            else:
                expr = f"COALESCE(CAST(TRIM({col}) AS STRING), '<<NULL>>')"
        else:  # athena / trino
            if "timestamp" in dt or "date" == dt:
                expr = f"COALESCE(CAST(date_format({col}, '%Y-%m-%d %H:%i:%s') AS VARCHAR), '<<NULL>>')"
            elif "boolean" in dt:
                expr = f"COALESCE(CAST(CASE WHEN {col} THEN '1' ELSE '0' END AS VARCHAR), '<<NULL>>')"
            else:
                expr = f"COALESCE(CAST(TRIM({col}) AS VARCHAR), '<<NULL>>')"
        parts.append(expr)

    concat_expr = sep.join(parts)
    if db_type in ("postgresql", "postgres"):
        hash_expr = f"MD5({concat_expr})"
    elif db_type in ("mssql", "sqlserver"):
        hash_expr = f"LOWER(CONVERT(VARCHAR(32), HASHBYTES('MD5', {concat_expr}), 2))"
    elif db_type == "snowflake":
        hash_expr = f"MD5({concat_expr})"
    else:
        hash_expr = f"MD5({concat_expr})"

    return f"SELECT {hash_expr} AS row_hash\nFROM {table_fqn}"


# =============================================================================
# TAB: Custom SQL Validation
# DQE writes their own source + target SQL (any join, grain, aggregation, etc.)
# for N validations, picks PK columns, and we write the YAML directly.
# No AI column-mapping involved — DQE owns the query.
# =============================================================================
with tab_custom:
    import yaml as _yaml
    import pandas as pd

    st.markdown("""
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:4px;">
        <div style="width:40px;height:40px;border-radius:10px;
                    background:linear-gradient(135deg,#4F46E5,#818CF8);
                    display:flex;align-items:center;justify-content:center;font-size:1.2rem;">✍️</div>
        <div>
            <div style="font-size:1.25rem;font-weight:800;color:#0F172A;">Custom SQL Validation</div>
            <div style="font-size:0.8rem;color:#64748B;">
                Write your own source + Snowflake SQL for any grain, join, or business logic.
                Add as many validations as you need — each becomes one entry in the generated YAML.
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.info(
        "Use this tab when the standard column-mapping flow isn't enough — e.g. monthly grain aggregations, "
        "multi-table joins, dedup checks, GL line item rollups, or any DQ rule expressed as SQL. "
        "You write both the source and Snowflake queries; we build the YAML.",
        icon="💡",
    )

    # ── Step 1 — Connection ───────────────────────────────────────────────────
    with st.container(border=True):
        st.markdown("**① Source connection & database/schema**")
        cst_registry = load_registry()
        cst_rec = select_connection(cst_registry, key="cst_conn")

        if cst_rec:
            _override_source_env(cst_rec)
            cst_db_type = cst_rec["db_type"]
            cst_password = source_password(cst_rec)

            if cst_db_type == "athena":
                cst_database = cst_rec["database"]
                cst_schema = cst_rec["schema"]
                st.caption(f"Athena: using Glue database **{cst_database}** (fixed from .env)")
            else:
                cst_dbs = cached_source_databases(
                    cst_db_type, cst_rec["host"], int(cst_rec.get("port") or 0),
                    cst_rec["username"], cst_password, cst_rec.get("auth", ""),
                )
                _cc1, _cc2 = st.columns(2)
                with _cc1:
                    cst_database = select_or_type("Source database", cst_dbs, cst_rec["database"], "cst_db")
                cst_schemas = cached_source_schemas(
                    cst_db_type, cst_rec["host"], int(cst_rec.get("port") or 0),
                    cst_database, cst_rec["username"], cst_password, cst_rec.get("auth", ""),
                )
                with _cc2:
                    cst_schema = select_or_type("Source schema", cst_schemas, cst_rec["schema"], "cst_schema")
        else:
            cst_rec = None
            cst_database = cst_schema = cst_db_type = ""

    # ── Step 2 — Snowflake target connection ──────────────────────────────────
    with st.container(border=True):
        st.markdown("**② Snowflake target database/schema**")
        cst_sf_database, cst_sf_schema, _ = pick_snowflake_target("", "cst_sf", include_table=False)

    # ── Step 3 — Layer ────────────────────────────────────────────────────────
    with st.container(border=True):
        st.markdown("**③ Medallion layer**")
        cst_layer, cst_output_dir = pick_layer("cst_layer")

    # Session-state list of validation entries — defined here so both the
    # AI generator (Step 4) and the manual entry grid (Step 5) can access it.
    _CST_KEY = "cst_entries"
    if _CST_KEY not in st.session_state:
        st.session_state[_CST_KEY] = []

    def _cst_blank_entry(idx: int) -> dict:
        return {
            "id": idx,
            "name": f"validation_{idx + 1}",
            "description": "",
            "source_sql": "",
            "target_sql": "",
            "pk_source": "",
            "pk_target": "",
            "validation_type": "data",
        }

    # ── Step 4 — AI SQL Generator (schema-aware) ─────────────────────────────
    st.markdown("**④ AI SQL Generator — describe what you need, AI writes the SQL**")
    st.caption(
        "Select tables from the source schema, describe your query in plain English "
        "(joins across tables, aggregations, grain, DQ checks — anything). "
        "The AI sees the full column schema of every selected table and writes "
        "dialect-correct SQL for your source DB. Generated SQL is added to the "
        "manual entries below for you to review and optionally pair with a Snowflake query."
    )

    with st.container(border=True):
        # Only active once a connection + schema are picked
        _ai_ready = bool(cst_rec and cst_schema)

        if not _ai_ready:
            st.info("Select a source connection and schema above (Steps ① and ②) to enable AI SQL generation.", icon="👆")
        else:
            # ── Table selector ───────────────────────────────────────────────
            try:
                _ai_tables = cached_source_tables(
                    cst_db_type, cst_rec["host"], int(cst_rec.get("port") or 0),
                    cst_database, cst_rec["username"], source_password(cst_rec),
                    cst_rec.get("auth", ""), cst_rec.get("s3_output", ""), cst_schema,
                )
            except Exception as _exc:
                _ai_tables = []
                st.warning(f"Could not list tables: {_exc}")

            # shared model picker above both sections
            ai_model = select_or_type(
                "AI model", available_models_for_ui(),
                os.getenv("DIAL_MODEL", "gpt-4o"),
                "cst_ai_model", format_func=_model_label,
            )

            # shared prompt + validation name
            ai_prompt = st.text_area(
                "Describe the SQL you need (plain English — same description drives both source and target generation)",
                key="cst_ai_prompt",
                height=100,
                placeholder=(
                    "Examples:\n"
                    "• Employees with department name, location, budget and salary (base + total) ordered by salary desc.\n"
                    "• Monthly total sales by region — only active customers, grain = month + region.\n"
                    "• Find duplicate email addresses across the contacts table."
                ),
            )

            _ai_validation_name = st.text_input(
                "Validation name",
                value="ai_generated_check",
                key="cst_ai_val_name",
                placeholder="e.g. employee_salary_check",
            )

            st.markdown("---")

            _ai_normalize = st.radio(
                "Query style",
                options=["Validation query (COALESCE / <<NULL>> normalized)", "Simple query (plain readable SQL)"],
                index=0,
                horizontal=True,
                key="cst_ai_normalize",
                help=(
                    "**Validation query** — adds COALESCE(CAST(...), '<<NULL>>') to every column so "
                    "the comparison engine can detect NULLs vs empty strings. Use this when you will "
                    "paste the result into the YAML and run validation.\n\n"
                    "**Simple query** — clean readable SQL, no normalization wrappers. Use this to "
                    "explore data or hand-write your own normalization."
                ),
            )
            _ai_do_normalize = "Simple" not in _ai_normalize

            # ── Two side-by-side generation sections ─────────────────────────
            _AI_SQL_KEY = "cst_ai_result_sql"
            _SF_SQL_KEY = "cst_ai_sf_result_sql"

            _sec_src, _sec_sf = st.columns(2)

            # ── LEFT: Source SQL generation ───────────────────────────────────
            with _sec_src:
                st.markdown(f"**🗄️ Source SQL — {cst_db_type.upper()}**")
                ai_selected_tables = st.multiselect(
                    "Tables to include",
                    options=_ai_tables,
                    key="cst_ai_tables",
                    placeholder="Pick source tables…",
                )
                # Column preview + filter — only show when exactly one table picked
                _src_col_filter = {}
                if ai_selected_tables:
                    for _pt in ai_selected_tables:
                        try:
                            _all_cols = cached_source_columns(
                                cst_db_type, cst_rec["host"], int(cst_rec.get("port") or 0),
                                cst_database, cst_rec["username"], source_password(cst_rec),
                                cst_rec.get("auth", ""), cst_rec.get("s3_output", ""), cst_schema, _pt,
                            )
                            _picked_cols = st.multiselect(
                                f"Columns from {_pt} (leave blank = all)",
                                options=_all_cols,
                                key=f"cst_src_cols_{_pt}",
                                placeholder="All columns included by default",
                            )
                            _src_col_filter[_pt] = _picked_cols or _all_cols
                        except Exception:
                            pass

                _do_gen_src = st.button(
                    f"✨ Generate {cst_db_type.upper()} SQL",
                    type="primary",
                    key="cst_ai_generate",
                    disabled=not (ai_selected_tables and ai_prompt.strip()),
                )
                if not ai_selected_tables:
                    st.caption("Select at least one table to enable.")

                if _do_gen_src:
                    _schema_ctx = {}
                    with st.spinner("Loading source schema…"):
                        for _tbl in ai_selected_tables:
                            try:
                                _ext = ExtractorFactory.create(
                                    cst_db_type,
                                    host=cst_rec["host"],
                                    port=int(cst_rec.get("port") or 0),
                                    database=cst_database,
                                    username=cst_rec["username"],
                                    password=source_password(cst_rec),
                                    auth=cst_rec.get("auth", ""),
                                    s3_output=cst_rec.get("s3_output", ""),
                                )
                                _col_metas = _ext.extract_columns(cst_schema, _tbl)
                                _allowed = set(_src_col_filter.get(_tbl) or [c.column_name for c in _col_metas])
                                _schema_ctx[f"{cst_schema}.{_tbl}"] = [
                                    {
                                        "column_name": c.column_name,
                                        "data_type": c.data_type,
                                        "is_nullable": c.is_nullable,
                                        "is_primary_key": c.is_primary_key,
                                    }
                                    for c in _col_metas if c.column_name in _allowed
                                ]
                            except Exception as _exc:
                                st.warning(f"Could not load schema for {_tbl}: {_exc}")

                    if _schema_ctx:
                        # Detect PKs for comment + auto-fill
                        _src_pks = [
                            col["column_name"]
                            for tbl_cols in _schema_ctx.values()
                            for col in tbl_cols
                            if col.get("is_primary_key")
                        ]
                        with st.spinner("Generating source SQL…"):
                            try:
                                _gen = AISQLQueryGenerator(model=ai_model)
                                _result = _gen.generate_schema_aware_query(
                                    user_instruction=ai_prompt.strip(),
                                    schema_context=_schema_ctx,
                                    db_type=cst_db_type,
                                    default_schema=cst_schema,
                                    normalize=_ai_do_normalize,
                                )
                                _pk_comment = f"-- PK: {', '.join(_src_pks)}\n" if _src_pks else ""
                                # No PK detected → row-hash SQL only for single-table selections.
                                # Multi-table = join; the FROM can't be inferred, user must alias a PK in SQL.
                                _row_hash_sql = ""
                                if not _src_pks and len(_schema_ctx) == 1:
                                    _single_tbl_cols = next(iter(_schema_ctx.values()))
                                    _single_tbl_fqn  = next(iter(_schema_ctx))
                                    _row_hash_sql = _build_row_hash_sql(_single_tbl_cols, _single_tbl_fqn, cst_db_type)
                                st.session_state[_AI_SQL_KEY] = {
                                    "sql": _pk_comment + _result.query,
                                    "confidence": _result.confidence,
                                    "prompt": ai_prompt.strip(),
                                    "schema_ctx": _schema_ctx,
                                    "pks": _src_pks,
                                    "row_hash_sql": _row_hash_sql,
                                }
                            except AISQLGenerationError as _exc:
                                st.error(f"Source SQL generation failed: {_exc}")
                                st.session_state.pop(_AI_SQL_KEY, None)
                    else:
                        st.error("Could not load column schema — check connection.")

                _ai_result = st.session_state.get(_AI_SQL_KEY)
                if _ai_result:
                    st.markdown(
                        f"<div style='font-size:0.8rem;font-weight:600;color:#4F46E5;margin-top:8px;'>"
                        f"Generated {cst_db_type.upper()} SQL "
                        f"<span style='color:#059669;margin-left:6px;'>confidence {int(_ai_result['confidence']*100)}%</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                    _detected_src_pks = _ai_result.get("pks") or []
                    _src_pk_default = ", ".join(_detected_src_pks) if _detected_src_pks else "row_hash"
                    _src_pk_override = st.text_input(
                        "Detected Source PK — edit if needed (comma-separated for composite; 'row_hash' = no-PK mode)",
                        value=_src_pk_default,
                        key="cst_ai_src_pk_override",
                        help="Auto-detected from information_schema. 'row_hash' means no PK was found; a hash-based query is shown below.",
                    )
                    if not _detected_src_pks:
                        if _ai_result.get("row_hash_sql"):
                            st.warning("No primary key detected. Use the row-hash query below as your source SQL — it hashes every column so rows can be compared without a PK.")
                            st.code(_ai_result["row_hash_sql"], language="sql")
                        else:
                            st.warning("No primary key detected and multiple tables selected — row-hash requires a single table. Add an alias column in your SQL to use as PK (e.g. `ROW_NUMBER() OVER (...) AS row_id`).")
                    st.code(_ai_result["sql"], language="sql")
                    # Push the overridden PK back so "Add entry" can read it
                    _ai_result["_pk_override"] = _src_pk_override
                    if st.button("🔄 Regenerate source", key="cst_ai_regen_src"):
                        st.session_state.pop(_AI_SQL_KEY, None)
                        st.rerun()

            # ── RIGHT: Snowflake SQL generation ──────────────────────────────
            with _sec_sf:
                st.markdown("**❄️ Snowflake SQL (target)**")
                _sf_tbl_opts = []
                if cst_sf_database and cst_sf_schema:
                    try:
                        _sf_tbl_opts = cached_sf_tables(cst_sf_database, cst_sf_schema)
                    except Exception:
                        pass
                ai_selected_sf_tables = st.multiselect(
                    "Snowflake tables to include",
                    options=_sf_tbl_opts,
                    key="cst_ai_sf_tables",
                    placeholder="Pick Snowflake tables…" if _sf_tbl_opts else "Select Snowflake schema first (Step ②)",
                    disabled=not _sf_tbl_opts,
                )
                _sf_col_filter = {}
                if ai_selected_sf_tables:
                    for _sft in ai_selected_sf_tables:
                        try:
                            _sf_all_cols = cached_sf_columns(cst_sf_database, cst_sf_schema, _sft)
                            _sf_picked = st.multiselect(
                                f"Columns from {_sft} (leave blank = all)",
                                options=_sf_all_cols,
                                key=f"cst_sf_cols_{_sft}",
                                placeholder="All columns included by default",
                            )
                            _sf_col_filter[_sft] = _sf_picked or _sf_all_cols
                        except Exception:
                            pass

                _do_gen_sf = st.button(
                    "✨ Generate Snowflake SQL",
                    type="primary",
                    key="cst_ai_sf_generate",
                    disabled=not (ai_selected_sf_tables and ai_prompt.strip()),
                )
                if not _sf_tbl_opts:
                    st.caption("Complete Step ② (Snowflake schema) to enable.")
                elif not ai_selected_sf_tables:
                    st.caption("Select at least one Snowflake table to enable.")

                if _do_gen_sf:
                    _sf_schema_ctx = {}
                    with st.spinner("Loading Snowflake schema…"):
                        for _tbl in ai_selected_sf_tables:
                            try:
                                _sf_col_metas = SnowflakeExtractor(database=cst_sf_database).extract_columns(cst_sf_schema, _tbl)
                                _sf_allowed = set(_sf_col_filter.get(_tbl) or [c.column_name for c in _sf_col_metas])
                                _sf_schema_ctx[f"{cst_sf_schema}.{_tbl}"] = [
                                    {
                                        "column_name": c.column_name,
                                        "data_type": c.data_type,
                                        "is_nullable": c.is_nullable,
                                        "is_primary_key": c.is_primary_key,
                                    }
                                    for c in _sf_col_metas if c.column_name in _sf_allowed
                                ]
                            except Exception as _exc:
                                st.warning(f"Could not load Snowflake schema for {_tbl}: {_exc}")

                    if _sf_schema_ctx:
                        _sf_pks = [
                            col["column_name"]
                            for tbl_cols in _sf_schema_ctx.values()
                            for col in tbl_cols
                            if col.get("is_primary_key")
                        ]
                        with st.spinner("Generating Snowflake SQL…"):
                            try:
                                _sf_gen = AISQLQueryGenerator(model=ai_model)
                                _sf_result = _sf_gen.generate_schema_aware_query(
                                    user_instruction=ai_prompt.strip(),
                                    schema_context=_sf_schema_ctx,
                                    db_type="snowflake",
                                    default_schema=cst_sf_schema,
                                    normalize=_ai_do_normalize,
                                )
                                _sf_pk_comment = f"-- PK: {', '.join(_sf_pks)}\n" if _sf_pks else ""
                                _sf_row_hash_sql = ""
                                if not _sf_pks and len(_sf_schema_ctx) == 1:
                                    _sf_single_cols = next(iter(_sf_schema_ctx.values()))
                                    _sf_single_fqn  = next(iter(_sf_schema_ctx))
                                    _sf_row_hash_sql = _build_row_hash_sql(_sf_single_cols, _sf_single_fqn, "snowflake")
                                st.session_state[_SF_SQL_KEY] = {
                                    "sql": _sf_pk_comment + _sf_result.query,
                                    "confidence": _sf_result.confidence,
                                    "pks": _sf_pks,
                                    "row_hash_sql": _sf_row_hash_sql,
                                }
                            except AISQLGenerationError as _exc:
                                st.error(f"Snowflake SQL generation failed: {_exc}")
                                st.session_state.pop(_SF_SQL_KEY, None)
                    else:
                        st.error("Could not load Snowflake column schema — check connection.")

                _sf_ai_result = st.session_state.get(_SF_SQL_KEY)
                if _sf_ai_result:
                    st.markdown(
                        f"<div style='font-size:0.8rem;font-weight:600;color:#059669;margin-top:8px;'>"
                        f"Generated Snowflake SQL "
                        f"<span style='color:#64748B;margin-left:6px;'>confidence {int(_sf_ai_result['confidence']*100)}%</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                    _detected_sf_pks = _sf_ai_result.get("pks") or []
                    _sf_pk_default = ", ".join(_detected_sf_pks) if _detected_sf_pks else "row_hash"
                    _sf_pk_override = st.text_input(
                        "Detected Snowflake PK — edit if needed",
                        value=_sf_pk_default,
                        key="cst_ai_sf_pk_override",
                        help="Auto-detected from Snowflake information_schema. 'row_hash' = no-PK hash mode.",
                    )
                    if not _detected_sf_pks:
                        if _sf_ai_result.get("row_hash_sql"):
                            st.warning("No PK detected on Snowflake side. Use the row-hash query below.")
                            st.code(_sf_ai_result["row_hash_sql"], language="sql")
                        else:
                            st.warning("No PK detected and multiple Snowflake tables selected — row-hash requires a single table.")
                    st.code(_sf_ai_result["sql"], language="sql")
                    _sf_ai_result["_pk_override"] = _sf_pk_override
                    if st.button("🔄 Regenerate Snowflake", key="cst_ai_regen_sf"):
                        st.session_state.pop(_SF_SQL_KEY, None)
                        st.rerun()

            st.markdown("---")

            # ── Add as validation entry ───────────────────────────────────────
            _ai_result = st.session_state.get(_AI_SQL_KEY)
            _sf_ai_result = st.session_state.get(_SF_SQL_KEY)
            _can_add = bool(_ai_result or _sf_ai_result)
            if _can_add:
                if st.button("➕ Add as validation entry below", key="cst_ai_add_entry", type="primary"):
                    nxt = len(st.session_state.get(_CST_KEY, []))
                    new_entry = _cst_blank_entry(nxt)
                    new_entry["name"] = _ai_validation_name.strip().replace(" ", "_").lower() or f"ai_check_{nxt+1}"
                    new_entry["description"] = f"AI-generated — {(_ai_result or _sf_ai_result or {}).get('prompt', ai_prompt)[:80]}"
                    if _ai_result:
                        # Use row-hash SQL when no PK was found and the user kept 'row_hash'
                        _src_pk_val = _ai_result.get("_pk_override") or ", ".join(_ai_result.get("pks") or [])
                        if _src_pk_val.strip().lower() == "row_hash" and _ai_result.get("row_hash_sql"):
                            new_entry["source_sql"] = _ai_result["row_hash_sql"]
                        else:
                            new_entry["source_sql"] = _ai_result["sql"]
                        new_entry["pk_source"] = _src_pk_val
                    if _sf_ai_result:
                        _sf_pk_val = _sf_ai_result.get("_pk_override") or ", ".join(_sf_ai_result.get("pks") or [])
                        if _sf_pk_val.strip().lower() == "row_hash" and _sf_ai_result.get("row_hash_sql"):
                            new_entry["target_sql"] = _sf_ai_result["row_hash_sql"]
                        else:
                            new_entry["target_sql"] = _sf_ai_result["sql"]
                        new_entry["pk_target"] = _sf_pk_val
                    if _CST_KEY not in st.session_state:
                        st.session_state[_CST_KEY] = []
                    st.session_state[_CST_KEY].append(new_entry)
                    st.session_state.pop(_AI_SQL_KEY, None)
                    st.session_state.pop(_SF_SQL_KEY, None)
                    _sides = []
                    if _ai_result:
                        _sides.append("source")
                    if _sf_ai_result:
                        _sides.append("Snowflake")
                    flash(f"Added '{new_entry['name']}' ({' + '.join(_sides)} SQL) — review the entry below.", icon="✅")
                    st.rerun()

    # ── Step 5 — Validation entries ───────────────────────────────────────────
    st.markdown("**⑤ Validation entries**")
    st.caption(
        "Add one entry per logical check. Each entry gets its own YAML block. "
        "Source SQL runs against your source DB; Snowflake SQL runs against your Snowflake target. "
        "The PK column(s) are used to align rows for comparison — use the same logical key in both queries."
    )

    # Add / remove buttons
    _btn_c1, _btn_c2 = st.columns([1, 5])
    with _btn_c1:
        if st.button("➕ Add validation", key="cst_add"):
            nxt = len(st.session_state[_CST_KEY])
            st.session_state[_CST_KEY].append(_cst_blank_entry(nxt))
            st.rerun()

    if not st.session_state[_CST_KEY]:
        st.markdown("""
        <div style="background:#F8FAFC;border:2px dashed #CBD5E1;border-radius:12px;
                    padding:32px;text-align:center;color:#94A3B8;margin:16px 0;">
            <div style="font-size:2rem;margin-bottom:8px;">📝</div>
            <div style="font-size:0.95rem;font-weight:600;">No validations yet</div>
            <div style="font-size:0.82rem;margin-top:4px;">
                Click <strong>➕ Add validation</strong> above to write your first SQL check.
            </div>
        </div>
        """, unsafe_allow_html=True)

    to_delete = []
    for idx, entry in enumerate(st.session_state[_CST_KEY]):
        entry_key = f"cst_entry_{idx}"
        with st.expander(
            f"**{entry['name'] or f'Validation {idx+1}'}** — {entry.get('description','') or 'click to expand'}",
            expanded=True,
        ):
            _e1, _e2, _e3 = st.columns([3, 4, 1])
            with _e1:
                entry["name"] = st.text_input(
                    "Validation name (used as YAML key)",
                    value=entry["name"], key=f"{entry_key}_name",
                    placeholder="e.g. monthly_sales_grain",
                )
            with _e2:
                entry["description"] = st.text_input(
                    "Description (optional)",
                    value=entry["description"], key=f"{entry_key}_desc",
                    placeholder="e.g. Monthly sales by region, joined with dim_customer",
                )
            with _e3:
                entry["validation_type"] = st.selectbox(
                    "Type", ["data", "count"], key=f"{entry_key}_vtype",
                    index=0 if entry["validation_type"] == "data" else 1,
                    help="data = row-level diff with PK alignment; count = row count only",
                )

            _s1, _s2 = st.columns(2)
            with _s1:
                st.markdown(
                    f"<div style='font-size:0.8rem;font-weight:600;color:#4F46E5;margin-bottom:4px;'>"
                    f"Source SQL ({_DB_TYPE_LABELS.get(cst_db_type, cst_db_type) or 'source'})"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                _src_hints = {
                    "mssql":      "SELECT\n    FORMAT(order_date,'yyyy-MM') AS month,\n    region,\n    SUM(amount) AS total_sales\nFROM dbo.sales s\nJOIN dbo.dim_customer c ON s.customer_id=c.id\nGROUP BY FORMAT(order_date,'yyyy-MM'), region",
                    "athena":     "SELECT\n    date_trunc('month', order_date) AS month,\n    region,\n    SUM(amount) AS total_sales\nFROM schema.sales s\nJOIN schema.dim_customer c ON s.customer_id=c.id\nGROUP BY 1, 2",
                    "postgresql": "SELECT\n    DATE_TRUNC('month', order_date) AS month,\n    region,\n    SUM(amount)                    AS total_sales\nFROM sales s\nJOIN dim_customer c ON s.customer_id=c.id\nGROUP BY 1, 2",
                }
                entry["source_sql"] = st.text_area(
                    "Source SQL",
                    value=entry["source_sql"], key=f"{entry_key}_src_sql",
                    height=220,
                    placeholder=_src_hints.get(cst_db_type, _src_hints["postgresql"]),
                    label_visibility="collapsed",
                )
            with _s2:
                st.markdown(
                    "<div style='font-size:0.8rem;font-weight:600;color:#059669;margin-bottom:4px;'>"
                    "Snowflake SQL (target)"
                    "</div>",
                    unsafe_allow_html=True,
                )
                entry["target_sql"] = st.text_area(
                    "Snowflake SQL",
                    value=entry["target_sql"], key=f"{entry_key}_tgt_sql",
                    height=220,
                    placeholder=(
                        "SELECT\n"
                        "    DATE_TRUNC('month', ORDER_DATE) AS MONTH,\n"
                        "    REGION,\n"
                        "    SUM(AMOUNT)                     AS TOTAL_SALES\n"
                        "FROM DWH.SALES S\n"
                        "JOIN DWH.DIM_CUSTOMER C ON S.CUSTOMER_ID = C.ID\n"
                        "GROUP BY 1, 2"
                    ),
                    label_visibility="collapsed",
                )

            if entry["validation_type"] == "data":
                _pk1, _pk2 = st.columns(2)
                with _pk1:
                    entry["pk_source"] = st.text_input(
                        "Source PK column(s) — comma-separated if composite",
                        value=entry["pk_source"], key=f"{entry_key}_pk_src",
                        placeholder="e.g. month, region",
                    )
                with _pk2:
                    entry["pk_target"] = st.text_input(
                        "Snowflake PK column(s) — must align with source PK",
                        value=entry["pk_target"], key=f"{entry_key}_pk_tgt",
                        placeholder="e.g. MONTH, REGION",
                    )

            if st.button("🗑️ Remove this validation", key=f"{entry_key}_del", type="secondary"):
                to_delete.append(idx)

    if to_delete:
        for idx in sorted(to_delete, reverse=True):
            st.session_state[_CST_KEY].pop(idx)
        st.rerun()

    # ── Step 6 — YAML preview + save ─────────────────────────────────────────
    st.divider()
    entries = st.session_state.get(_CST_KEY, [])
    valid_entries = [e for e in entries if e.get("source_sql", "").strip() and e.get("target_sql", "").strip() and e.get("name", "").strip()]

    if valid_entries and cst_rec:
        _tables_dict = {}
        for e in valid_entries:
            vname = e["name"].strip().replace(" ", "_")
            block_type = "data_validation" if e["validation_type"] == "data" else "count_validation"
            inner = {
                "source_table_name": vname,
                "source": cst_db_type or "postgresql",
                "source_database": cst_database,
                "source_schema": cst_schema,
                "sourcequery": " ".join(e["source_sql"].split()),
                "target_table_name": vname,
                "target": "snowflake",
                "target_database": cst_sf_database,
                "target_schema": cst_sf_schema,
                "targetquery": " ".join(e["target_sql"].split()),
            }
            if block_type == "data_validation":
                pk_src = [c.strip() for c in e["pk_source"].split(",") if c.strip()]
                pk_tgt = [c.strip() for c in e["pk_target"].split(",") if c.strip()]
                if pk_src:
                    inner["pksourcecolumn"] = pk_src if len(pk_src) > 1 else pk_src[0]
                if pk_tgt:
                    inner["pktargetcolumn"] = pk_tgt if len(pk_tgt) > 1 else pk_tgt[0]
            _tables_dict[vname] = {"validations": {block_type: inner}}

        vtype_folder = "data_validation"
        yaml_stem = valid_entries[0]["name"].strip().replace(" ", "_") if len(valid_entries) == 1 else "custom_validations"
        yaml_payload = {"tables": _tables_dict}

        yaml_str = _yaml.dump(yaml_payload, default_flow_style=False, sort_keys=False, allow_unicode=True, Dumper=_yaml.SafeDumper)

        # ── Inline schema validation (no tempfile — we already have the dict) ─
        from validation.config_schema import ValidationConfigDocument
        from pydantic import ValidationError as _PydanticValidationError
        _yaml_errors = []
        try:
            ValidationConfigDocument.model_validate(yaml_payload)
        except _PydanticValidationError as _ve:
            _yaml_errors = [
                f"{'.'.join(str(p) for p in e['loc'])} — {e['msg']}"
                for e in _ve.errors()
            ]

        with st.expander("📄 YAML preview", expanded=True):
            st.code(yaml_str, language="yaml")
            if _yaml_errors:
                st.error("**Schema errors — fix before saving:**")
                for _msg in _yaml_errors:
                    st.markdown(f"- `{_msg}`")
            else:
                st.success("✓ YAML passes schema validation", icon="✅")

        _save_c1, _save_c2, _save_c3 = st.columns([2, 2, 3])
        with _save_c1:
            custom_yaml_filename = st.text_input(
                "Output filename (without .yaml)",
                value=yaml_stem, key="cst_yaml_filename",
            )
        with _save_c2:
            report_subfolder = st.text_input(
                "Report subfolder (optional)",
                value="", key="cst_report_subfolder",
                placeholder="e.g. emanagement",
                help="If set, saves under config/report/<subfolder>/data_validation/ (independent of layer). Leave blank to save under config/<layer>/data_validation/.",
            )
        with _save_c3:
            st.markdown("<div style='height:28px'/>", unsafe_allow_html=True)
            if st.button("💾 Save YAML to config folder", type="primary", key="cst_save",
                         disabled=bool(_yaml_errors)):
                if report_subfolder.strip():
                    save_dir = _ROOT_DIR / "Project" / "config" / "report" / report_subfolder.strip() / vtype_folder
                else:
                    save_dir = cst_output_dir / vtype_folder
                save_path = save_dir / f"{custom_yaml_filename}.yaml"
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_text(yaml_str, encoding="utf-8")
                flash(f"Saved: {save_path}", icon="💾")
                st.rerun()

    elif entries and not valid_entries:
        st.warning("Fill in at least a name + source SQL + Snowflake SQL to preview the YAML.")
    elif not entries:
        pass  # empty state already shown above
    elif not cst_rec:
        st.warning("Select a source connection above before saving.")

# =============================================================================
# TAB: Run Validation — execute the generated YAMLs (Project/main.py) and
# show pass/fail results, without ever displaying raw row values.
# =============================================================================
with tab_execute:
    st.subheader("Run validation — pick the YAML file(s) to execute")
    st.caption(
        "Runs `Project/main.py` against the YAML configs generated in the tabs above, "
        "then reads back the run's summary — counts and pass/fail status only."
    )

    import pandas as pd

    _exec_top1, _exec_top2 = st.columns(2)
    with _exec_top1:
        layer = st.selectbox("Medallion layer", _LAYERS, index=0, key="exec_layer")
    with _exec_top2:
        environment = st.selectbox("Environment", ["local", "dev", "uat", "prod"], key="exec_env")

    # ── Build full YAML inventory (all layers + report/) ─────────────────────
    # Each row: {stem, vtype, folder, layer, path}
    # Scans every layer so the folder-filter multiselect below does the narrowing.
    def _build_exec_inventory():
        import yaml as _yi
        rows = []
        for _ly in _LAYERS:
            # Count validation — one YAML per source DB (mssql.yaml, postgres.yaml, …)
            # Fall back to legacy {layer}.yaml for older runs
            _cv_dir = _PROJECT_DIR / "config" / _ly / "count_validation"
            _cv_yamls = sorted(_cv_dir.glob("*.yaml")) if _cv_dir.exists() else []
            for _cv_path in _cv_yamls:
                _source_db = _cv_path.stem  # filename IS the source db name
                _cfg = _yi.safe_load(_cv_path.read_text(encoding="utf-8")) or {}
                for tbl, tbl_block in (_cfg.get("tables") or {}).items():
                    # Also read source field from YAML for legacy files where stem != db type
                    _src = ((tbl_block.get("validations") or {})
                            .get("count_validation", {})
                            .get("source") or _source_db)
                    rows.append({"stem": tbl, "vtype": "count_validation",
                                 "folder": _ly, "layer": _ly, "path": _cv_path,
                                 "source_db": _src.lower()})
            # Data validation — subdir per source DB: data_validation/{source_db}/{table}.yaml
            # Also handles legacy flat layout: data_validation/{table}.yaml
            _dv_dir = _PROJECT_DIR / "config" / _ly / "data_validation"
            if _dv_dir.exists():
                for p in sorted(_dv_dir.rglob("*.yaml")):
                    # subdir layout: p.parent.name = source_db, p.parent.parent.name = "data_validation"
                    # flat layout: p.parent.name = "data_validation"
                    if p.parent.name == "data_validation":
                        _src_db = "unknown"
                    else:
                        _src_db = p.parent.name  # e.g. "mssql", "postgres"
                    rows.append({"stem": p.stem, "vtype": "data_validation",
                                 "folder": _ly, "layer": _ly, "path": p,
                                 "source_db": _src_db.lower()})
        # Report/ data_validation — independent of layer
        _rep_root = _PROJECT_DIR / "config" / "report"
        if _rep_root.exists():
            for p in sorted(_rep_root.rglob("*.yaml")):
                if p.parent.name == "data_validation" or p.parent.parent.name == "data_validation":
                    subfolder = p.parent.parent.name if p.parent.parent.name != "data_validation" else p.parent.parent.parent.name
                    _src_db = p.parent.name if p.parent.parent.name == "data_validation" else "unknown"
                    rows.append({"stem": p.stem, "vtype": "data_validation",
                                 "folder": f"report/{subfolder}", "layer": "bronze", "path": p,
                                 "source_db": _src_db.lower()})
        return rows

    _inventory = _build_exec_inventory()

    if not _inventory:
        st.warning(
            f"No YAML configs found for layer '{layer}' yet — generate one first "
            f"in the **Generate Single/Batch YAML** tabs."
        )
    else:
        # ── Filters row ──────────────────────────────────────────────────────
        _fc1, _fc2, _fc3, _fc4 = st.columns([2, 2, 2, 3])
        with _fc1:
            _all_folders = sorted({r["folder"] for r in _inventory})
            _sel_folders = st.multiselect(
                "Filter by folder", options=_all_folders,
                default=_all_folders, key="exec_folder_filter",
                help="'bronze/silver/gold' = layer configs. 'report/X' = custom report configs.",
            )
        with _fc2:
            _vtype_opts = sorted({r["vtype"] for r in _inventory})
            _sel_vtypes = st.multiselect(
                "Validation type", options=_vtype_opts,
                default=_vtype_opts, key="exec_vtype_filter",
            )
        with _fc3:
            _all_src_dbs = sorted({r["source_db"] for r in _inventory})
            _sel_src_dbs = st.multiselect(
                "Source DB", options=_all_src_dbs,
                default=_all_src_dbs, key="exec_source_db_filter",
                help="Filter by source database type (mssql, postgres, athena, redshift, …).",
            )
        with _fc4:
            _search = st.text_input(
                "Search tables", placeholder="Type to filter…", key="exec_search",
            )

        _filtered = [
            r for r in _inventory
            if r["folder"] in (_sel_folders or _all_folders)
            and r["vtype"] in (_sel_vtypes or _vtype_opts)
            and r["source_db"] in (_sel_src_dbs or _all_src_dbs)
            and (_search.strip().lower() in r["stem"].lower() if _search.strip() else True)
        ]

        select_all = st.checkbox("Select all", value=True, key="exec_select_all")
        _sel_set_key = f"exec_sel_{layer}_{select_all}"

        file_rows = [
            {"Run": select_all, "Table": r["stem"], "Source DB": r["source_db"],
             "Type": r["vtype"], "Folder": r["folder"],
             "File": str(r["path"].relative_to(_PROJECT_DIR))}
            for r in _filtered
        ]
        files_df = pd.DataFrame(file_rows) if file_rows else pd.DataFrame(
            columns=["Run", "Table", "Type", "Folder", "File"])

        st.caption(f"{len(files_df)} of {len(_inventory)} config(s) shown — check the ones to run.")
        edited = st.data_editor(
            files_df,
            column_config={
                "Run":    st.column_config.CheckboxColumn(help="Include in run"),
                "Table":     st.column_config.TextColumn(disabled=True),
                "Source DB": st.column_config.TextColumn(disabled=True),
                "Type":      st.column_config.TextColumn(disabled=True),
                "Folder":    st.column_config.TextColumn(disabled=True),
                "File":      st.column_config.TextColumn(disabled=True),
            },
            hide_index=True, width="stretch", key=f"exec_file_grid_{_sel_set_key}",
        )

        picked_count_tables = edited.loc[
            (edited["Type"] == "count_validation") & edited["Run"], "Table"
        ].tolist()
        picked_data_tables = edited.loc[
            (edited["Type"] == "data_validation") & edited["Run"], "Table"
        ].tolist()
        do_count = bool(picked_count_tables)
        do_data = bool(picked_data_tables)
        selected_tables = sorted(set(picked_count_tables) | set(picked_data_tables))

        if selected_tables and set(picked_count_tables) != set(picked_data_tables) and do_count and do_data:
            st.info(
                "Count and data validation have different table selections — a table checked for only one "
                "type will be silently skipped for the other (no matching YAML requested for it)."
            )

        _thresh_col, _count_thresh_col = st.columns([2, 2])
        with _thresh_col:
            mismatch_threshold = st.number_input(
                "Acceptable mismatch % (0 = exact match required)",
                min_value=0.0, max_value=100.0, value=0.0, step=0.1,
                format="%.2f", key="exec_threshold",
                help="e.g. 0.10 means ≤0.10% mismatched rows = PASS. Written into the YAML before running.",
            )
        with _count_thresh_col:
            count_mismatch_threshold = st.number_input(
                "Acceptable count difference % (0 = exact match required)",
                min_value=0.0, max_value=100.0, value=0.0, step=0.1,
                format="%.2f", key="exec_count_threshold",
                help="For tables under active CDC/replication, a strict row-count match will "
                     "intermittently FAIL for no real reason. e.g. 0.05 means ≤0.05% count "
                     "difference = PASS. Written into the YAML before running.",
            )

        # Derive run layer from the actual inventory rows selected (first match wins)
        _picked_stems = set(selected_tables)
        _run_layer = next(
            (r["layer"] for r in _filtered if r["stem"] in _picked_stems),
            layer,
        )

        if st.button("🚀 Run validation", type="primary", key="exec_run", disabled=not selected_tables):
            # Inject mismatch_threshold_pct into data_validation YAMLs before running
            if mismatch_threshold > 0:
                import yaml as _yrun
                # Build stem→path map from inventory directly (covers all layers + report)
                _all_data_yamls = {r["stem"]: r["path"] for r in _inventory if r["vtype"] == "data_validation"}
                for _tbl in picked_data_tables:
                    _yp = _all_data_yamls.get(_tbl)
                    if _yp.exists():
                        try:
                            _ydoc = _yrun.safe_load(_yp.read_text(encoding="utf-8")) or {}
                            for _tentry in (_ydoc.get("tables") or {}).values():
                                _dv = (_tentry.get("validations") or {}).get("data_validation")
                                if _dv:
                                    _dv["mismatch_threshold_pct"] = mismatch_threshold
                            _yp.write_text(_yrun.dump(_ydoc, default_flow_style=False, sort_keys=False, allow_unicode=True), encoding="utf-8")
                        except Exception:
                            pass

            # Inject count_mismatch_threshold_pct into the layer's single count_validation
            # YAML before running — count_validation has no tolerance by default (strict ==),
            # so this is opt-in per run, same pattern as the data_validation threshold above.
            if count_mismatch_threshold > 0 and picked_count_tables:
                import yaml as _yrun
                _cv_yaml = _PROJECT_DIR / "config" / layer / "count_validation" / f"{layer}.yaml"
                if _cv_yaml.exists():
                    try:
                        _cvdoc = _yrun.safe_load(_cv_yaml.read_text(encoding="utf-8")) or {}
                        for _tbl in picked_count_tables:
                            _tentry = (_cvdoc.get("tables") or {}).get(_tbl)
                            _cv = (_tentry.get("validations") or {}).get("count_validation") if _tentry else None
                            if _cv:
                                _cv["count_mismatch_threshold_pct"] = count_mismatch_threshold
                        _cv_yaml.write_text(_yrun.dump(_cvdoc, default_flow_style=False, sort_keys=False, allow_unicode=True), encoding="utf-8")
                    except Exception:
                        pass

            with st.spinner(f"Running {_run_layer} validation against '{environment}' — this executes real queries..."):
                try:
                    result = run_validation(_run_layer, environment, selected_tables, do_count, do_data)
                except Exception as exc:
                    st.error(f"Execution failed to start: {exc}")
                    result = None

            if result:
                if result["run_id"] and result["summaries"]:
                    st.success(f"Run complete — run_id `{result['run_id']}`  ·  exit code {result['returncode']}")
                elif result["run_id"]:
                    st.error(
                        f"Run `{result['run_id']}` finished (exit code {result['returncode']}) but produced no "
                        f"summary — every table validation errored before completing. See log below."
                    )
                else:
                    st.error("Run did not produce a run_id — see raw output below.")

                for vtype, df in result["summaries"].items():
                    with st.container(border=True):
                        st.markdown(f"#### {'🔢' if vtype == 'count_validation' else '🧬'} {vtype.replace('_', ' ').title()} summary")
                        n_total = len(df)
                        n_pass = int((df["status"] == "PASS").sum())
                        n_fail = n_total - n_pass
                        m1, m2, m3 = st.columns(3)
                        m1.metric("Tables checked", n_total)
                        m2.metric("Passed", n_pass)
                        m3.metric("Failed", n_fail, delta=-n_fail if n_fail else None, delta_color="inverse")
                        show_failed_only = st.checkbox(
                            "Show failed only", value=False, key=f"exec_summary_failed_only_{vtype}"
                        )
                        display_df = df[df["status"] == "FAIL"] if show_failed_only else df
                        render_paginated_df(display_df, key_prefix=f"exec_summary_{vtype}")
                        st.download_button(
                            f"⬇ Download {'failed-only ' if show_failed_only else ''}{vtype} summary CSV",
                            data=display_df.to_csv(index=False).encode("utf-8"),
                            file_name=f"{vtype}_summary{'_failed' if show_failed_only else ''}.csv",
                            mime="text/csv",
                            key=f"dl_summary_{vtype}",
                        )

                if result["diff_files"]:
                    with st.container(border=True):
                        st.markdown("#### 🔍 Data validation — row-level results")
                        st.caption("Every row is shown — green = matched, red = differed. Local files only, never sent to any AI/LLM.")
                        for f in result["diff_files"]:
                            _render_diff_file(f, key_prefix=f"exec_diff_{f.stem}")

                if result.get("failed_files"):
                    with st.container(border=True):
                        st.markdown("#### ❌ Data validation — failed rows only")
                        st.caption("Same rows as above, pre-filtered to mismatches. Download below to share just the failures.")
                        for f in result["failed_files"]:
                            _render_diff_file(f, key_prefix=f"exec_failed_{f.stem}")
                            st.download_button(
                                f"⬇ Download {f.name}",
                                data=f.read_bytes(),
                                file_name=f.name,
                                mime="text/csv",
                                key=f"dl_failed_{f.stem}",
                            )

                if not result["summaries"]:
                    with st.expander("Raw stdout/stderr (no summary was produced)", expanded=True):
                        st.code(result["stdout_tail"] or "(empty)")
                        if result["stderr_tail"]:
                            st.code(result["stderr_tail"])
                elif result["returncode"] != 0:
                    with st.expander("Raw stdout/stderr (non-zero exit — some tables may have errored)", expanded=False):
                        st.code(result["stdout_tail"] or "(empty)")
                        if result["stderr_tail"]:
                            st.code(result["stderr_tail"])

# =============================================================================
# TAB: History & Trends — SQLite-backed validation history (results_store.py),
# populated automatically by every run in the Run Validation tab. Replaces
# manually grepping through Project/output/<layer>/validation_<run_id>/ CSVs.
# =============================================================================
with tab_history:
    st.subheader("Validation history")
    st.caption("Every run from the Run Validation tab is recorded here — counts and pass/fail status only.")

    hist_layer_choice = st.selectbox("Layer", ["All"] + list(_LAYERS), index=0, key="hist_layer")
    hist_layer = None if hist_layer_choice == "All" else hist_layer_choice

    runs_df = results_store.query_runs(layer=hist_layer, limit=50)
    if runs_df.empty:
        st.info("No runs recorded yet — run a validation in the **Run Validation** tab first.")
    else:
        total_checks = int(runs_df["checks"].sum())
        total_passed = int(runs_df["passed"].sum())
        total_failed = int(runs_df["failed"].sum())
        pass_rate = round(100 * total_passed / total_checks, 1) if total_checks else 0
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Runs recorded", len(runs_df))
        m2.metric("Total checks", total_checks)
        m3.metric("Passed", total_passed)
        m4.metric("Failed", total_failed, delta=-total_failed if total_failed else None, delta_color="inverse")
        m5.metric("Pass rate", f"{pass_rate}%")

        with st.container(border=True):
            st.markdown("#### 🗂️ Recent runs")
            render_paginated_df(
                runs_df[["run_id", "layer", "environment", "returncode", "recorded_at", "checks", "passed", "failed"]],
                key_prefix="hist_runs", style_status=False,
            )

        st.divider()
        with st.container(border=True):
            st.markdown("#### 📉 Drill down by table")
            tables = results_store.distinct_tables(layer=hist_layer)
            if not tables:
                st.caption("No per-table results yet.")
            else:
                picked_table = st.selectbox("Table", tables, key="hist_table")
                trend_df = results_store.table_trend(picked_table)
                if not trend_df.empty:
                    trend_df = trend_df.sort_values("run_at")
                    c1, c2 = st.columns(2)
                    c1.metric("Runs for this table", len(trend_df))
                    c2.metric("Latest status", trend_df.iloc[-1]["status"])
                    count_trend = trend_df.dropna(subset=["source_count", "target_count"])
                    if not count_trend.empty:
                        st.line_chart(
                            count_trend.set_index("run_at")[["source_count", "target_count"]],
                        )
                    render_paginated_df(
                        trend_df[["run_id", "validation_type", "status", "source_count", "target_count",
                                  "count_difference", "run_at"]],
                        key_prefix="hist_trend",
                    )

        st.divider()
        with st.container(border=True):
            st.markdown("#### 🔎 Filter all results")
            f1, f2, f3 = st.columns(3)
            with f1:
                status_filter = st.selectbox("Status", ["All", "PASS", "FAIL"], key="hist_status")
            with f2:
                vtype_filter = st.selectbox("Validation type", ["All", "count_validation", "data_validation"], key="hist_vtype")
            with f3:
                table_filter = st.selectbox("Table", ["All"] + tables, key="hist_table_filter")

            results_df = results_store.query_results(
                layer=hist_layer,
                status=None if status_filter == "All" else status_filter,
                validation_type=None if vtype_filter == "All" else vtype_filter,
                table=None if table_filter == "All" else table_filter,
            )
            render_paginated_df(results_df, key_prefix="hist_results")
            st.download_button(
                "⬇ Download filtered results CSV",
                data=results_df.to_csv(index=False).encode("utf-8"),
                file_name=f"validation_history_{status_filter.lower()}.csv",
                mime="text/csv",
                key="dl_hist_results",
            )

# =============================================================================
# TAB: Rule Book
# =============================================================================
with tab_rules:
    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown("""
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:4px;">
        <div style="width:40px;height:40px;border-radius:10px;
                    background:linear-gradient(135deg,#4F46E5,#818CF8);
                    display:flex;align-items:center;justify-content:center;font-size:1.2rem;">📖</div>
        <div>
            <div style="font-size:1.25rem;font-weight:800;color:#0F172A;">Rule Book</div>
            <div style="font-size:0.8rem;color:#64748B;">Type-mapping normalization rules — base (always on) and learned (activate to enable)</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Stat chips ────────────────────────────────────────────────────────────
    stats = rule_book.stats()
    _rs1, _rs2, _rs3, _rs4 = st.columns(4)
    _rs1.metric("Base rules", stats["base_rules"], help="Code-defined, always run first")
    _rs2.metric("Learned — active", sum(1 for r in rule_book.learned_rules() if r.status == "active"),
                help="Gap fillers — only for type pairs without a base rule")
    _rs3.metric("Learned — draft", sum(1 for r in rule_book.learned_rules() if r.status != "active"),
                help="Advisory only — never affect generated SQL until activated")
    _rs4.metric("Total", stats["total_rules"])

    # ── Concept explainer ─────────────────────────────────────────────────────
    st.markdown("""
    <div style="background:#F0F4FF;border:1px solid #C7D2FE;border-radius:10px;padding:14px 18px;margin:12px 0;">
    <div style="font-weight:700;color:#3730A3;margin-bottom:8px;">📌 How rules work</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;font-size:0.82rem;color:#334155;">
        <div style="background:white;border-radius:8px;padding:10px 12px;border:1px solid #E0E7FF;">
            <div style="font-weight:700;color:#059669;margin-bottom:4px;">🔒 Base rule</div>
            Built into <code>base_rules.py</code>. Always runs first for a type pair.
            <b>Nothing can shadow or override it.</b>
        </div>
        <div style="background:white;border-radius:8px;padding:10px 12px;border:1px solid #E0E7FF;">
            <div style="font-weight:700;color:#6366F1;margin-bottom:4px;">📝 Draft rule</div>
            Saved but not active. Fed to AI as context only.
            <b>Never affects real SQL generation.</b>
        </div>
        <div style="background:white;border-radius:8px;padding:10px 12px;border:1px solid #E0E7FF;">
            <div style="font-weight:700;color:#D97706;margin-bottom:4px;">⚡ Active rule</div>
            A reviewed draft you activated. Acts as a <b>gap filler</b> for type pairs
            with no base rule — cannot replace base rules.
        </div>
    </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Sub-tabs ───────────────────────────────────────────────────────────────
    _rb_base_tab, _rb_learned_tab, _rb_add_tab = st.tabs(
        ["🔒 Base Rules", "⚡ Learned Rules", "➕ Add Rule"]
    )

    # ── BASE RULES sub-tab ────────────────────────────────────────────────────
    with _rb_base_tab:
        st.caption(
            "Read-only — always checked first for any type pair. The SQL shown is the **actual expression** "
            "run against real data. Read it, don't just the description."
        )

        st.markdown("""
        <div style="background:#FFFBEB;border:1px solid #FDE68A;border-radius:8px;padding:12px 16px;margin-bottom:12px;font-size:0.83rem;color:#78350F;">
        <b>Key rules to know:</b>
        &nbsp;·&nbsp; <b>Numeric/Decimal</b> — cast to text at native precision (no rounding — drift surfaces as FAIL).
        &nbsp;·&nbsp; <b>Timestamp TZ</b> — converted to UTC first, then microsecond-formatted.
        &nbsp;·&nbsp; <b>UUID</b> — UPPER(TRIM()) normalised — genuine case differences still FAIL.
        </div>
        """, unsafe_allow_html=True)

        # Search filter
        _rb_search = st.text_input("🔍 Filter rules", placeholder="e.g. uuid, timestamp, numeric…", key="rb_search", label_visibility="collapsed")

        def rule_rows(entries):
            rows = []
            for e in entries:
                sample_col = "amount" if "numeric" in e.id or "integer" in e.id else "col"
                rows.append({
                    "ID": e.id, "Name": e.display_name,
                    "Source type": e.source_type, "Target type": e.target_type,
                    "Source SQL": e.pg_sql_template.replace("{col}", sample_col) if e.pg_sql_template else "",
                    "Snowflake SQL": e.sf_sql_template.replace("{col}", sample_col) if e.sf_sql_template else "",
                    "Description": e.description,
                })
            return rows

        _base = rule_book.base_rules()
        if _rb_search:
            _q = _rb_search.lower()
            _base = [r for r in _base if _q in r.id.lower() or _q in (r.source_type or "").lower()
                     or _q in (r.display_name or "").lower()]

        st.dataframe(
            rule_rows(_base),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Source SQL":    st.column_config.TextColumn(width="large"),
                "Snowflake SQL": st.column_config.TextColumn(width="large"),
                "Description":   st.column_config.TextColumn(width="medium"),
            },
        )

    # ── LEARNED RULES sub-tab ─────────────────────────────────────────────────
    with _rb_learned_tab:
        learned = rule_book.learned_rules()
        if not learned:
            st.markdown("""
            <div style="text-align:center;padding:48px 24px;color:#94A3B8;">
                <div style="font-size:2rem;margin-bottom:8px;">📭</div>
                <div style="font-weight:600;font-size:1rem;margin-bottom:4px;">No learned rules yet</div>
                <div style="font-size:0.82rem;">Use the <b>Add Rule</b> tab to paste a type-mapping table
                or fill in the form manually.</div>
            </div>
            """, unsafe_allow_html=True)
        else:
            _active_rules  = [r for r in learned if r.status == "active"]
            _draft_rules   = [r for r in learned if r.status != "active"]

            if _active_rules:
                st.markdown("##### ⚡ Active — used as gap fillers")
                for r in _active_rules:
                    with st.container(border=True):
                        _la, _lb, _lc, _ld = st.columns([3, 2, 2, 1])
                        with _la:
                            st.markdown(f"`{r.id}`")
                            st.caption(r.description or "")
                        _lb.markdown(f"**{r.source_type}** → **{r.target_type}**")
                        _lc.markdown(f"Reuses `{r.reuses_rule}`" if r.reuses_rule else "_No base rule_")
                        with _ld:
                            st.markdown('<span class="badge badge-active">ACTIVE</span>', unsafe_allow_html=True)
                            if st.button("Deactivate", key=f"deact_{r.id}", type="secondary"):
                                rule_book.deactivate_learned_rule(r.id)
                                st.rerun()

            if _draft_rules:
                st.markdown("##### 📝 Draft — advisory only (activate to use)")
                for r in _draft_rules:
                    with st.container(border=True):
                        _la, _lb, _lc, _ld = st.columns([3, 2, 2, 1])
                        with _la:
                            st.markdown(f"`{r.id}`")
                            st.caption(r.description or "")
                        _lb.markdown(f"**{r.source_type}** → **{r.target_type}**")
                        _lc.markdown(f"Reuses `{r.reuses_rule}`" if r.reuses_rule else "_No base rule_")
                        with _ld:
                            st.markdown('<span class="badge badge-draft">DRAFT</span>', unsafe_allow_html=True)
                            if r.reuses_rule:
                                if st.button("Activate", key=f"act_{r.id}", type="primary"):
                                    rule_book.activate_learned_rule(r.id)
                                    flash(f"'{r.id}' is now active — gap filler for {r.source_type} → {r.target_type}.", icon="✅")
                                    st.rerun()
                            else:
                                st.caption("advisory only")

    # ── ADD RULE sub-tab ──────────────────────────────────────────────────────
    with _rb_add_tab:
        _add_ai_tab, _add_manual_tab = st.tabs(["✨ AI-assisted paste", "✏️ Manual form"])

        with _add_ai_tab:
            st.markdown("""
            <div style="font-size:0.85rem;color:#475569;margin-bottom:10px;">
            Paste any type-mapping table or free text — e.g. <code>nvarchar → TEXT</code>, or a full
            MSSQL/Postgres → Snowflake mapping list. The AI <b>can only reuse an existing base rule's
            SQL</b> — it cannot invent new SQL. Rows it can't match are flagged for manual resolution.
            </div>
            """, unsafe_allow_html=True)

            raw_rules_text = st.text_area(
                "Paste type mappings", height=130, key="rule_paste_text",
                placeholder="bit (0,1)      -> BOOLEAN\nmoney          -> NUMBER\nnvarchar       -> TEXT\ntimestamp      -> BINARY",
                label_visibility="collapsed",
            )
            _rp_col1, _rp_col2 = st.columns([3, 1])
            with _rp_col1:
                rule_parse_model = select_or_type(
                    "AI model", available_models_for_ui(), os.getenv("DIAL_MODEL", "gpt-4o"),
                    "rule_parse_model", format_func=_model_label,
                )
            with _rp_col2:
                st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                if st.button("✨ Parse with AI", key="parse_rules_btn", type="primary", use_container_width=True):
                    if not raw_rules_text.strip():
                        st.error("Paste some type mappings first.")
                    else:
                        with st.spinner("Parsing pasted rules…"):
                            try:
                                parser = RuleTypeParser(model=rule_parse_model)
                                proposals = parser.parse(raw_rules_text, rule_book.base_rule_ids())
                                st.session_state["rule_proposals"] = proposals
                            except (AIRuleMappingError, RuleParseError) as exc:
                                st.error(f"Could not parse: {exc}")
                                st.session_state.pop("rule_proposals", None)

            proposals = st.session_state.get("rule_proposals")
            if proposals:
                import pandas as pd

                def _covered(source_type: str, target_type: str) -> bool:
                    from rules import get_rule_for_type_specific
                    return get_rule_for_type_specific(source_type, target_type) is not None

                rows = []
                for p in proposals:
                    covered = _covered(p.source_type, p.target_type)
                    status = "already covered" if covered else ("needs review" if p.needs_review else "new — will save")
                    rows.append({
                        "Save": (not covered) and not p.needs_review,
                        "Source type": p.source_type, "Target type": p.target_type,
                        "Dialect": p.dialect, "Reuses rule": p.reuses_rule or "",
                        "Confidence": p.confidence, "Status": status, "Note": p.note,
                    })
                df = pd.DataFrame(rows)
                st.markdown(f"**{len(proposals)} mapping(s) parsed** — check rows to save as draft rules:")
                edited = st.data_editor(
                    df,
                    column_config={
                        "Save": st.column_config.CheckboxColumn(help="Only rows with a matched base rule and no review flag can be saved."),
                        "Source type": st.column_config.TextColumn(disabled=True),
                        "Target type": st.column_config.TextColumn(disabled=True),
                        "Dialect": st.column_config.TextColumn(disabled=True),
                        "Reuses rule": st.column_config.SelectboxColumn(options=[""] + rule_book.base_rule_ids()),
                        "Confidence": st.column_config.NumberColumn(disabled=True, format="%.2f"),
                        "Status": st.column_config.TextColumn(disabled=True),
                        "Note": st.column_config.TextColumn(disabled=True),
                    },
                    hide_index=True, use_container_width=True, key="rule_proposal_editor",
                )

                if st.button("💾 Save checked rows as draft rules", key="save_rule_proposals", type="primary"):
                    import datetime as _dt
                    import re as _re
                    saved, skipped = 0, 0
                    for _, row in edited.iterrows():
                        if not row["Save"]:
                            continue
                        if not row["Reuses rule"]:
                            skipped += 1
                            continue
                        slug = _re.sub(r"[^a-z0-9]+", "_", f"{row['Dialect']}_{row['Source type']}_{row['Target type']}".lower()).strip("_")
                        entry = RuleEntry(
                            id=f"prompt_{slug}",
                            display_name=f"{row['Source type']} -> {row['Target type']} ({row['Dialect']})",
                            description=row["Note"] or f"Reuses '{row['Reuses rule']}' rule via AI-assisted paste.",
                            when_to_apply=f"source_type={row['Source type']}, target_type={row['Target type']}, dialect={row['Dialect']}",
                            pg_sql_template="", sf_sql_template="",
                            source_type=row["Source type"], target_type=row["Target type"],
                            reuses_rule=row["Reuses rule"],
                            learned_at=_dt.date.today().isoformat(),
                        )
                        try:
                            if rule_book.save_learned_rule(entry):
                                saved += 1
                        except RuleValidationError as exc:
                            st.error(f"'{row['Source type']} → {row['Target type']}' rejected: {exc}")
                            skipped += 1
                    if saved:
                        flash(f"Saved {saved} rule(s) as draft — activate them in the Learned Rules tab.", icon="📖")
                        st.session_state.pop("rule_proposals", None)
                        st.rerun()
                    elif skipped:
                        st.warning("Nothing saved — check that at least one row has 'Save' checked and a 'Reuses rule' chosen.")

        with _add_manual_tab:
            st.caption("New rules start as **draft** (advisory only) — go to Learned Rules and click Activate to make them live gap fillers.")
            with st.form("add_rule_form", border=False):
                _mf1, _mf2 = st.columns(2)
                rule_id      = _mf1.text_input("Rule ID (snake_case)", placeholder="mssql_money_to_number")
                display_name = _mf2.text_input("Display name", placeholder="MONEY → NUMBER")
                description  = st.text_area("Description", height=80, placeholder="What this rule does and when to apply it")
                when_to_apply = st.text_input("When to apply", placeholder="source=MONEY, target=NUMBER, dialect=mssql")
                _mf3, _mf4   = st.columns(2)
                source_type  = _mf3.text_input("Source type", placeholder="MONEY")
                target_type  = _mf4.text_input("Target type", placeholder="NUMBER")
                _mf5, _mf6   = st.columns(2)
                pg_sql_template = _mf5.text_input("Source SQL template", placeholder="CAST({col} AS NUMERIC)")
                sf_sql_template = _mf6.text_input("Snowflake SQL template", placeholder="CAST({col} AS NUMBER)")
                submitted = st.form_submit_button("💾 Save as draft rule", type="primary")

                if submitted:
                    if not rule_id or not display_name:
                        st.error("Rule ID and display name are required.")
                    else:
                        import datetime
                        entry = RuleEntry(
                            id=rule_id, display_name=display_name, description=description,
                            when_to_apply=when_to_apply, pg_sql_template=pg_sql_template,
                            sf_sql_template=sf_sql_template, source_type=source_type,
                            target_type=target_type, is_learned=True,
                            learned_at=datetime.date.today().isoformat(),
                        )
                        try:
                            ok = rule_book.save_learned_rule(entry)
                        except RuleValidationError as exc:
                            st.error(f"Rejected: {exc}")
                            ok = None
                        if ok:
                            flash(f"Learned rule '{rule_id}' saved — activate it in the Learned Rules tab.", icon="📖")
                            st.rerun()
                        elif ok is not None:
                            st.error("Could not save — a rule with this ID may already exist.")

# =============================================================================
# TAB: Exclusions
# =============================================================================
with tab_excl:
    st.subheader("Per-source exclusion policy")
    st.caption("One file per source type — every entry applies to BOTH that source and the Snowflake target.")

    for db_type in SOURCE_TYPES:
        label = _DB_TYPE_LABELS.get(db_type, db_type)
        path = _exclusions_path_for(db_type)
        with st.expander(f"{label}  —  {path.name}", expanded=False):
            all_excl = _get_all_exclusions(db_type)
            static_set = {c.lower() for c in STATIC_EXCLUDE_COLUMNS}
            user_excl = sorted(c for c in all_excl if c not in static_set)
            st.write("**Static (built-in, cannot be removed here):**", ", ".join(STATIC_EXCLUDE_COLUMNS))
            st.write("**User-saved global exclusions:**")
            if user_excl:
                for col in user_excl:
                    rc1, rc2 = st.columns([5, 1])
                    rc1.write(f"`{col}`")
                    if rc2.button("🗑️ Remove", key=f"remove_excl_{db_type}_{col}"):
                        if _remove_global_user_exclusion(db_type, col):
                            flash(f"Removed '{col}' from {label} exclusions.", icon="🗑️")
                            st.rerun()
                        else:
                            st.error(f"Could not remove '{col}' — it may already be gone.")
            else:
                st.caption("(none)")

    st.divider()
    st.markdown("**Add a new global exclusion**")
    with st.form("add_exclusion_form"):
        col_names = st.text_input("Column name(s), comma-separated")
        reason = st.text_input("Reason", value="User-defined global exclusion")
        targets = st.multiselect(
            "Applies to source type(s)",
            options=[_DB_TYPE_LABELS.get(t, t) for t in SOURCE_TYPES],
            default=[],
        )
        submitted = st.form_submit_button("Save exclusion")

        if submitted:
            cols = [c.strip() for c in col_names.split(",") if c.strip()]
            label_to_type = {_DB_TYPE_LABELS.get(t, t): t for t in SOURCE_TYPES}
            chosen_types = [label_to_type[t] for t in targets]
            if not cols or not chosen_types:
                st.error("Enter at least one column name and pick at least one source type.")
            else:
                for db_type in chosen_types:
                    for col in cols:
                        _save_global_user_exclusion(db_type, col, reason)
                flash(f"Saved {len(cols)} column(s) to exclusions for {', '.join(targets)}", icon="🚫")
                st.rerun()

# =============================================================================
# TAB: Usage & Cost
# =============================================================================
_SCHED_KEY = "sched_job_id"
_is_sched_running = _SCHED_KEY in st.session_state
_sched_label = "⏰ Scheduled Runs  🟢 Active" if _is_sched_running else "⏰ Scheduled Runs"

with st.sidebar.expander(_sched_label, expanded=False):
    if _is_sched_running:
        _sched_info = st.session_state.get("sched_meta", {})
        st.markdown(
            f"**Status:** 🟢 Running\n\n"
            f"**Layer:** `{_sched_info.get('layer','—')}`  "
            f"**Env:** `{_sched_info.get('env','—')}`  \n"
            f"**Interval:** {_sched_info.get('interval','—')}"
        )
        if st.button("⏹ Stop scheduler", key="sched_stop", type="primary", use_container_width=True):
            try:
                st.session_state[_SCHED_KEY].shutdown(wait=False)
            except Exception:
                pass
            for _k in (_SCHED_KEY, "sched_meta"):
                st.session_state.pop(_k, None)
            flash("Scheduler stopped.", icon="⏹")
            st.rerun()
    else:
        st.markdown("**Auto-run validation on a fixed schedule.**  \n*App must stay open.*")
        st.divider()
        _sched_layer = st.selectbox("Layer", _LAYERS, key="sched_layer")
        _sched_env = st.selectbox("Environment", ["local", "dev", "uat", "prod"], key="sched_env")
        _sched_interval = st.selectbox(
            "Interval",
            ["Every 1 hour", "Every 6 hours", "Every 12 hours", "Daily (midnight)"],
            key="sched_interval",
        )
        _sched_notify = st.checkbox("🔔 Notify on failure (Slack/email)", value=True, key="sched_notify")

        _interval_map = {
            "Every 1 hour": 3600, "Every 6 hours": 21600,
            "Every 12 hours": 43200, "Daily (midnight)": 86400,
        }

        if st.button("▶ Start scheduler", key="sched_start", type="primary", use_container_width=True):
            try:
                from apscheduler.schedulers.background import BackgroundScheduler
                _scheduler = BackgroundScheduler()
                _secs = _interval_map[_sched_interval]
                _snap_layer, _snap_env, _snap_notify = _sched_layer, _sched_env, _sched_notify

                def _scheduled_run():
                    try:
                        _r = run_validation(_snap_layer, _snap_env, ["all"], True, True)
                        if _snap_notify and _r.get("returncode", 0) != 0:
                            from notifier import notify_failure
                            notify_failure(
                                subject=f"[Scheduled] Validation failure — {_snap_layer}",
                                body=f"Run ID: {_r.get('run_id','?')}\n{_r.get('stdout_tail','')[-500:]}",
                            )
                    except Exception:
                        pass

                _scheduler.add_job(_scheduled_run, "interval", seconds=_secs)
                _scheduler.start()
                st.session_state[_SCHED_KEY] = _scheduler
                st.session_state["sched_meta"] = {
                    "layer": _sched_layer, "env": _sched_env, "interval": _sched_interval,
                }
                flash(f"Scheduler started — {_sched_interval.lower()}.", icon="⏰")
                st.rerun()
            except ImportError:
                st.error("Run `pip install apscheduler` to enable scheduled runs.")

# Usage & Cost moved to tab_usage — see below

# =============================================================================
# FLOATING WIDGET: Gemini Chat — fixed bottom-right bubble, not a tab.
# Toggle button + panel are two separate CSS-fixed containers (identified by
# Streamlit's `key=` -> `.st-key-<key>` class) so the panel's visibility can
# be flipped by injecting different CSS each rerun, without needing to nest
# the body under an extra `if`/indent level.
# =============================================================================
st.markdown("""
<style>
.st-key-gemini_chat_toggle {
    position: fixed !important; bottom: 24px !important; right: 24px !important;
    z-index: 10000 !important; left: auto !important;
    width: 64px !important; height: 64px !important;
}
.st-key-gemini_chat_toggle div[data-testid="stVerticalBlock"] { gap: 0; }
.st-key-gemini_chat_toggle button {
    border-radius: 50% !important; width: 64px !important; height: 64px !important;
    min-width: 64px !important; padding: 0 !important;
    font-size: 1.6rem !important; line-height: 1 !important;
    background: linear-gradient(135deg, #6C5CE7 0%, #A29BFE 100%) !important;
    color: white !important; border: none !important;
    box-shadow: 0 4px 16px rgba(108,92,231,0.45) !important;
    transition: transform 0.15s ease, box-shadow 0.15s ease !important;
}
.st-key-gemini_chat_toggle button:hover {
    transform: scale(1.08) !important;
    box-shadow: 0 6px 20px rgba(108,92,231,0.6) !important;
}
.st-key-gemini_chat_panel {
    position: fixed !important; bottom: 100px !important; right: 24px !important;
    z-index: 9999 !important; left: auto !important;
    max-width: 92vw !important; max-height: 85vh !important;
    overflow-y: auto;
    background: var(--background-color, white); border-radius: 16px;
    box-shadow: 0 10px 32px rgba(0,0,0,0.28); padding: 0;
    border: 1px solid rgba(128,128,128,0.2);
}
/* Drag handle on the left edge */
#gemini-resize-handle {
    position: absolute; left: 0; top: 0; bottom: 0; width: 6px;
    cursor: ew-resize; z-index: 10000;
    border-radius: 16px 0 0 16px;
    background: transparent;
    transition: background 0.15s;
}
#gemini-resize-handle:hover { background: rgba(108,92,231,0.25); }
.st-key-gemini_chat_size_btn {
    position: fixed !important; z-index: 10001 !important; left: auto !important;
}
.st-key-gemini_chat_size_btn button {
    width: 28px !important; height: 28px !important; min-width: 28px !important;
    padding: 0 !important; border-radius: 50% !important;
    background: rgba(255,255,255,0.25) !important; color: white !important;
    border: none !important; font-size: 0.85rem !important; line-height: 1 !important;
}
.gemini-chat-header {
    background: linear-gradient(135deg, #4F46E5 0%, #7C3AED 100%);
    color: white; padding: 14px 18px; border-radius: 16px 16px 0 0;
    display: flex; align-items: center; gap: 12px;
    box-shadow: 0 2px 8px rgba(79,70,229,.3);
}
.gemini-chat-header .gemini-logo {
    width: 36px; height: 36px; border-radius: 50%;
    background: rgba(255,255,255,0.18);
    border: 1.5px solid rgba(255,255,255,0.35);
    display: flex; align-items: center;
    justify-content: center; font-size: 1.15rem; flex-shrink: 0;
}
.gemini-chat-header .gemini-title { font-weight: 800; font-size: 1rem; letter-spacing: -.01em; }
.gemini-chat-header .gemini-subtitle { font-size: 0.73rem; opacity: 0.85; margin-top: 1px; }
.gemini-online-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: #34D399; box-shadow: 0 0 0 2px rgba(52,211,153,.35);
    flex-shrink: 0; margin-left: auto;
}
.gemini-chat-body { padding: 12px 16px; }
.gemini-status-bar {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 0 10px; flex-wrap: wrap;
}
.gemini-status-badge {
    display: inline-flex; align-items: center; gap: 5px;
    border-radius: 999px; padding: 4px 10px;
    font-size: 0.73rem; font-weight: 600; white-space: nowrap;
}
.gsb-success { background:#ECFDF5; color:#065F46; border:1px solid #6EE7B7; }
.gsb-warning { background:#FFFBEB; color:#92400E; border:1px solid #FDE68A; }
.gsb-info    { background:#EEF2FF; color:#3730A3; border:1px solid #A5B4FC; }
.gsb-neutral { background:#F1F5F9; color:#475569; border:1px solid #CBD5E1; }
/* Chat message bubble overrides */
[data-testid="stChatMessage"] {
    padding: 6px 0 !important;
    gap: 8px !important;
    background: transparent !important;
}
[data-testid="stChatMessageContent"] > div > p {
    margin-bottom: 4px !important;
    font-size: 0.875rem !important;
    line-height: 1.55 !important;
}
[data-testid="stChatMessage"][data-role="user"] [data-testid="stChatMessageContent"] {
    background: linear-gradient(135deg,#4F46E5,#7C3AED) !important;
    color: white !important; border-radius: 16px 16px 4px 16px !important;
    padding: 10px 14px !important; box-shadow: 0 2px 8px rgba(79,70,229,.28) !important;
}
[data-testid="stChatMessage"][data-role="user"] [data-testid="stChatMessageContent"] p { color: white !important; }
[data-testid="stChatMessage"][data-role="assistant"] [data-testid="stChatMessageContent"] {
    background: white !important; border: 1px solid #E2E8F0 !important;
    border-radius: 16px 16px 16px 4px !important;
    padding: 10px 14px !important; box-shadow: 0 1px 4px rgba(0,0,0,.07) !important;
}
/* Quick-action chip buttons */
.st-key-gemini_chat_panel .stButton button[kind="secondary"] {
    border-radius: 999px !important; font-size: 0.76rem !important;
    padding: 5px 12px !important; font-weight: 600 !important;
    background: #EEF2FF !important; color: #4338CA !important;
    border: 1.5px solid #A5B4FC !important;
    transition: background .15s, box-shadow .15s !important;
}
.st-key-gemini_chat_panel .stButton button[kind="secondary"]:hover {
    background: #E0E7FF !important; box-shadow: 0 2px 8px rgba(79,70,229,.2) !important;
}
</style>
""", unsafe_allow_html=True)

if "_gemini_chat_open" not in st.session_state:
    st.session_state["_gemini_chat_open"] = False
if "_gemini_chat_size" not in st.session_state:
    st.session_state["_gemini_chat_size"] = "default"

with st.container(key="gemini_chat_toggle"):
    _toggle_label = "✕" if st.session_state["_gemini_chat_open"] else "✨"
    if st.button(_toggle_label, key="gemini_chat_toggle_btn", help="Migration Intelligence chat"):
        st.session_state["_gemini_chat_open"] = not st.session_state["_gemini_chat_open"]
        st.rerun()

# Horizontal drag-to-resize via a JS handle injected into the panel.
# JS reads/writes localStorage so the user's chosen width survives reruns.
st.markdown("""
<script>
(function() {
  function initResize() {
    const panel = document.querySelector('.st-key-gemini_chat_panel');
    if (!panel) return;
    if (panel.querySelector('#gemini-resize-handle')) return;

    // Restore saved width
    const saved = localStorage.getItem('gemini_chat_w');
    if (saved) panel.style.setProperty('width', saved + 'px', 'important');

    const handle = document.createElement('div');
    handle.id = 'gemini-resize-handle';
    panel.style.position = 'fixed';
    panel.appendChild(handle);

    let startX, startW;
    handle.addEventListener('mousedown', function(e) {
      e.preventDefault();
      startX = e.clientX;
      startW = panel.getBoundingClientRect().width;
      document.addEventListener('mousemove', onDrag);
      document.addEventListener('mouseup', stopDrag);
    });

    function onDrag(e) {
      // Dragging left = wider (panel anchored to right edge)
      const newW = Math.min(Math.max(280, startW + (startX - e.clientX)), window.innerWidth * 0.92);
      panel.style.setProperty('width', newW + 'px', 'important');
    }

    function stopDrag() {
      const w = Math.round(panel.getBoundingClientRect().width);
      localStorage.setItem('gemini_chat_w', w);
      document.removeEventListener('mousemove', onDrag);
      document.removeEventListener('mouseup', stopDrag);
    }
  }

  // Streamlit re-renders the DOM; re-attach after each mutation.
  new MutationObserver(initResize).observe(document.body, {childList: true, subtree: true});
  initResize();
})();
</script>
""", unsafe_allow_html=True)

_GEMINI_CHAT_SIZES = {"default": (400, 520), "large": (640, 760)}
_gemini_size = st.session_state["_gemini_chat_size"]
_gemini_w, _gemini_h = _GEMINI_CHAT_SIZES.get(_gemini_size, _GEMINI_CHAT_SIZES["default"])
_gemini_btn_bottom = 100 + _gemini_h - 36
_gemini_btn_right = 24 + _gemini_w - 36
st.markdown(
    f"<style>"
    f".st-key-gemini_chat_panel {{ width: {_gemini_w}px !important; height: {_gemini_h}px !important; }}"
    f".st-key-gemini_chat_size_btn {{ bottom: {_gemini_btn_bottom}px !important; right: {_gemini_btn_right}px !important; }}"
    f"</style>",
    unsafe_allow_html=True,
)

if not st.session_state["_gemini_chat_open"]:
    st.markdown(
        '<style>.st-key-gemini_chat_panel, .st-key-gemini_chat_size_btn '
        '{ display: none !important; }</style>',
        unsafe_allow_html=True,
    )
else:
    with st.container(key="gemini_chat_size_btn"):
        _size_label = "⤡" if _gemini_size == "large" else "⤢"
        if st.button(_size_label, key="gemini_chat_size_btn_inner", help="Toggle chat panel size"):
            st.session_state["_gemini_chat_size"] = "default" if _gemini_size == "large" else "large"
            st.rerun()

with st.container(key="gemini_chat_panel"):
    # ── Header ─────────────────────────────────────────────────────────────
    sys.path.insert(0, str(_SRC_DIR))
    from connector.agent import create_agent

    _dial_key     = os.getenv("DIAL_API_KEY", "")
    _claude_key   = os.getenv("CLAUDE_API_KEY", "")
    _chat_ready   = bool(_dial_key or _claude_key)
    _auth_mode    = os.getenv("AUTH_MODE", "static").upper()
    _connector_ok = bool(os.getenv("CONNECTOR_API_TOKEN") or _auth_mode == "DEV")

    if _dial_key:
        _ai_backend = "DIAL · " + os.getenv("DIAL_MODEL", "gpt-4o")
        _ai_status  = "success"
    elif _claude_key:
        _ai_backend = "Claude · " + os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
        _ai_status  = "success"
    else:
        _ai_backend = "Offline"
        _ai_status  = "warning"

    st.markdown(
        '<div class="gemini-chat-header">'
        '<div class="gemini-logo">✨</div>'
        '<div>'
        '<div class="gemini-title">Migration Intelligence</div>'
        '<div class="gemini-subtitle">AI assistant · 24 governed tools · human-approved writes</div>'
        '</div>'
        '<div class="gemini-online-dot" title="Ready"></div>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="gemini-chat-body">', unsafe_allow_html=True)

    # ── Compact status bar ─────────────────────────────────────────────────
    _ai_cls   = "gsb-success" if _ai_status == "success" else "gsb-warning"
    _ai_dot   = "🟢" if _ai_status == "success" else "🟡"
    _conn_cls = "gsb-success" if _connector_ok else "gsb-warning"
    _conn_lbl = "Connector ready" if _connector_ok else "Connector: token not set"
    _auth_cls = "gsb-info" if _auth_mode == "JWT" else "gsb-neutral"
    _auth_ico = "🔐" if _auth_mode == "JWT" else ("🔑" if _auth_mode == "STATIC" else "🧪")
    st.markdown(
        f'<div class="gemini-status-bar">'
        f'<span class="gemini-status-badge {_ai_cls}">{_ai_dot} {_ai_backend}</span>'
        f'<span class="gemini-status-badge {_conn_cls}">{"✅" if _connector_ok else "⚠️"} {_conn_lbl}</span>'
        f'<span class="gemini-status-badge {_auth_cls}">{_auth_ico} {_auth_mode}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Enterprise identity panel ──────────────────────────────────────────
    with st.expander("🪪 Session Identity & Permissions", expanded=not st.session_state.get("gemini_actor")):
        st.markdown(
            "All approval and write-back actions are attributed to your identity and written "
            "to the immutable audit trail. Enter your corporate email to proceed."
        )
        _id_col1, _id_col2 = st.columns([2, 1])
        gemini_actor = _id_col1.text_input(
            "Corporate email / user ID",
            value=st.session_state.get("gemini_actor", ""),
            key="gemini_actor_input",
            placeholder="firstname.lastname@company.com",
            label_visibility="visible",
        )
        if gemini_actor:
            st.session_state["gemini_actor"] = gemini_actor

        # Derive and display RBAC role from auth mode
        _role_map = {"JWT": "Derived from JWT claims", "STATIC": os.getenv("CONNECTOR_ROLES", "ADMIN"), "DEV": "ADMIN (dev only)"}
        _role_display = _role_map.get(_auth_mode, os.getenv("CONNECTOR_ROLES", "ADMIN"))
        _id_col2.markdown(f"**RBAC Role**")
        _id_col2.code(_role_display, language=None)

        if gemini_actor:
            st.success(f"Identified as **{gemini_actor}** · role: {_role_display}", icon="🪪")
        else:
            st.warning("Identity required before any approval or write-back action is permitted.", icon="⚠️")

    if not _chat_ready:
        st.info(
            "Running in **offline mode** — tool dispatch is available but conversational AI "
            "requires `DIAL_API_KEY` in `.env` (current build/test backend), or "
            "`CLAUDE_API_KEY` once a direct Anthropic key is issued.",
            icon="ℹ️",
        )

    # Chat history
    if "gemini_messages" not in st.session_state:
        st.session_state["gemini_messages"] = []

    # Process pending prompt from quick action (must happen before rendering)
    if "gemini_pending_prompt" in st.session_state:
        _pending = st.session_state.pop("gemini_pending_prompt")
        st.session_state["gemini_messages"].append({"role": "user", "content": _pending})
        st.rerun()

    # ── Chat history fills the page ────────────────────────────────────────
    for msg in st.session_state["gemini_messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # ── Pending reply is rendered as a continuation of history, ABOVE the
    #    input box below — inside a tab, st.chat_input isn't reliably pinned
    #    to the viewport bottom, so DOM order determines what's on top. ─────
    msgs = st.session_state["gemini_messages"]
    if msgs and msgs[-1]["role"] == "user":
        last_user_msg = msgs[-1]["content"]
        with st.chat_message("assistant"):
            with st.spinner("Migration Intelligence agent is working…"):
                try:
                    sys.path.insert(0, str(_SRC_DIR))
                    from connector.agent import create_agent

                    agent_key = "agent_instance"
                    if agent_key not in st.session_state:
                        st.session_state[agent_key] = create_agent()
                    agent = st.session_state[agent_key]

                    if agent.is_configured():
                        result = agent.chat(last_user_msg, actor=st.session_state.get("gemini_actor", ""))
                    else:
                        result = agent.chat_offline(last_user_msg)

                    reply = result.get("text", "")
                    if not reply:
                        reply = "_No text response from the AI backend. Check API key and model configuration._"

                    st.markdown(reply)

                    # Show tool calls in expander
                    tool_calls = result.get("tool_calls", [])
                    if tool_calls:
                        with st.expander(f"🔧 Tools used ({len(tool_calls)} calls, {result.get('rounds', 0)} round(s))", expanded=False):
                            for tc in tool_calls:
                                st.markdown(f"**`{tc['name']}`**")
                                col_a, col_b = st.columns(2)
                                col_a.json(tc.get("args", {}))
                                col_b.json(tc.get("result", {}))

                    st.session_state["gemini_messages"].append({"role": "assistant", "content": reply})

                except ImportError as exc:
                    err_msg = f"Migration Intelligence connector not available: {exc}. Ensure the connector package is in src/."
                    st.error(err_msg)
                    st.session_state["gemini_messages"].append({"role": "assistant", "content": err_msg})
                except Exception as exc:
                    err_msg = f"Error: {exc}"
                    st.error(err_msg)
                    st.session_state["gemini_messages"].append({"role": "assistant", "content": err_msg})

    # ── Quick-action chips ─────────────────────────────────────────────────
    _quick_actions = [
        ("📋 Health",          "Give me a quick scorecard — how many tables are passing, failing, and need attention?"),
        ("🔍 Approvals",       "What column mappings need my sign-off? Show confidence scores and flag anything below 75%."),
        ("📊 ROI",             "Show me the automation rate, SQL scripts avoided, and failures caught so far."),
        ("🔌 Connections",     "What source databases are connected and which Snowflake target are they pointing to?"),
        ("📉 Coverage gaps",   "Which tables have validation coverage below 95%? Rank worst first."),
        ("⚡ Run & explain",   "Validate the migration_test table and explain any failures you find."),
    ]
    st.markdown("<div style='font-size:0.72rem;font-weight:700;color:#94A3B8;text-transform:uppercase;letter-spacing:.07em;margin-bottom:5px;'>Quick actions</div>", unsafe_allow_html=True)
    _qa_cols = st.columns(len(_quick_actions))
    for col, (label, prompt) in zip(_qa_cols, _quick_actions):
        if col.button(label, key=f"qa_{label}"):
            st.session_state["gemini_pending_prompt"] = prompt
            st.rerun()
    _cl1, _cl2 = st.columns([6, 1])
    with _cl2:
        if st.button("🗑️", key="gemini_clear", help="Clear conversation"):
            st.session_state["gemini_messages"] = []
            st.session_state.pop("agent_instance", None)
            st.rerun()

    # Plain form instead of st.chat_input — chat_input pins itself to the
    # true bottom of the whole app (full width), which breaks out of this
    # fixed-position floating panel. A form stays inside the panel's bounds.
    with st.form("gemini_chat_form", clear_on_submit=True, border=False):
        _fc1, _fc2 = st.columns([5, 1])
        user_input = _fc1.text_input("Ask anything — 'validate X', 'why did Y fail?', 'approve all high-confidence'…", key="gemini_input", label_visibility="collapsed")
        sent = _fc2.form_submit_button("➤")
    if sent and user_input:
        st.session_state["gemini_messages"].append({"role": "user", "content": user_input})
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)


# TAB: My Jira Tickets
# =============================================================================
with tab_jira:
    st.subheader("My Jira Tickets")
    st.caption("Tickets assigned to you in the configured Jira project — update status or attach a validation result.")

    try:
        from connector.jira_client import (
            is_configured, get_my_tickets, get_ticket,
            transition_ticket, add_comment, JiraError, JiraNotConfiguredError,
        )
    except ImportError:
        get_my_tickets = None

    if get_my_tickets is None or not is_configured():
        st.info("Jira not configured — add `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY` to `.env`.")
    else:
        _jql_filter = st.text_input(
            "Extra JQL filter (optional)",
            placeholder='e.g. priority = High AND labels = "data-migration"',
            key="jira_jql_extra",
        )
        _jira_refresh = st.button("🔄 Refresh tickets", key="jira_refresh")

        _JIRA_TICKETS_KEY = "jira_my_tickets"
        if _jira_refresh or _JIRA_TICKETS_KEY not in st.session_state:
            with st.spinner("Fetching your Jira tickets…"):
                try:
                    st.session_state[_JIRA_TICKETS_KEY] = get_my_tickets(_jql_filter.strip())
                except JiraError as _je:
                    st.error(f"Jira error: {_je}")
                    st.session_state[_JIRA_TICKETS_KEY] = []

        _tickets = st.session_state.get(_JIRA_TICKETS_KEY, [])
        if not _tickets:
            st.info("No open tickets assigned to you.")
        else:
            st.caption(f"{len(_tickets)} open ticket(s)")

            _STATUS_COLORS = {
                "To Do": "#94A3B8", "In Progress": "#F59E0B",
                "In Review": "#6366F1", "Done": "#10B981",
            }
            _PRIORITY_ICONS = {
                "Highest": "🔴", "High": "🟠", "Medium": "🟡",
                "Low": "🔵", "Lowest": "⚪",
            }

            for _tk in _tickets:
                _status_color = _STATUS_COLORS.get(_tk["status"], "#94A3B8")
                _priority_icon = _PRIORITY_ICONS.get(_tk["priority"], "")
                with st.expander(
                    f"{_priority_icon} **{_tk['key']}** — {_tk['summary']}",
                    expanded=False,
                ):
                    _tc1, _tc2 = st.columns([3, 1])
                    with _tc1:
                        st.markdown(
                            f"<span style='background:{_status_color};color:#fff;"
                            f"padding:2px 10px;border-radius:12px;font-size:0.78rem;'>"
                            f"{_tk['status']}</span> &nbsp; "
                            f"<a href='{_tk['url']}' target='_blank' style='font-size:0.82rem;'>"
                            f"Open in Jira ↗</a>",
                            unsafe_allow_html=True,
                        )

                    # ── Transition buttons ────────────────────────────────────
                    with _tc2:
                        _trans_target = (
                            "In Progress" if _tk["status"] == "To Do"
                            else "Done" if _tk["status"] == "In Progress"
                            else None
                        )
                        if _trans_target:
                            if st.button(
                                f"→ {_trans_target}",
                                key=f"jira_trans_{_tk['key']}",
                                type="primary",
                            ):
                                try:
                                    transition_ticket(_tk["key"], _trans_target)
                                    st.session_state.pop(_JIRA_TICKETS_KEY, None)
                                    flash(f"{_tk['key']} moved to '{_trans_target}'.", icon="✅")
                                    st.rerun()
                                except JiraError as _je:
                                    st.error(str(_je))

                    # ── Attach validation result as comment ───────────────────
                    with st.expander("📎 Attach validation result to this ticket", expanded=False):
                        _yaml_files = sorted(
                            (p for p in (_PROJECT_DIR / "config").rglob("*.yaml")
                             if p.parent.name in ("data_validation", "count_validation")),
                            key=lambda p: p.stat().st_mtime, reverse=True,
                        )
                        if not _yaml_files:
                            st.caption("No validation YAMLs found yet.")
                        else:
                            _sel_yaml = st.selectbox(
                                "Validation YAML",
                                options=_yaml_files,
                                format_func=lambda p: str(p),
                                key=f"jira_yaml_{_tk['key']}",
                            )
                            _note = st.text_area(
                                "Notes (optional)",
                                key=f"jira_note_{_tk['key']}",
                                height=68,
                            )
                            if st.button("📤 Post comment", key=f"jira_comment_{_tk['key']}"):
                                _comment = (
                                    f"[Migration Validator] Validation result attached: {_sel_yaml}\n"
                                    + (f"\n{_note}" if _note.strip() else "")
                                )
                                try:
                                    add_comment(_tk["key"], _comment)
                                    flash(f"Comment posted on {_tk['key']}.", icon="✅")
                                except JiraError as _je:
                                    st.error(str(_je))


# =============================================================================
# =============================================================================
# TAB: Usage & Cost
# =============================================================================
with tab_usage:
    import datetime as _dt
    from collections import defaultdict as _dd

    st.subheader("💰 AI Usage & Cost")
    st.caption(
        "Real token counts from every AI call (column mapping + SQL generation), "
        "logged to token_usage_analysis/logs/token_usage.jsonl. "
        "Cost estimated from public list prices — see token_usage_analysis/pricing.json."
    )

    _u_records = _load_token_records()
    _u_pricing = _load_pricing()

    if not _u_records:
        st.info("No AI calls logged yet. Run a Single YAML or Batch YAML generation with an AI key configured.")
    else:
        def _u_record_date(r: dict):
            try:
                return _dt.datetime.strptime(r["timestamp"], "%Y-%m-%dT%H:%M:%S").date()
            except Exception:
                return None

        _u_today = _dt.date.today()
        _u_last7 = _u_today - _dt.timedelta(days=6)
        _u_today_recs = [r for r in _u_records if _u_record_date(r) == _u_today]
        _u_week_recs  = [r for r in _u_records if (d := _u_record_date(r)) and _u_last7 <= d <= _u_today]

        def _u_totals(recs):
            tokens = sum(r.get("total_tokens", 0) for r in recs)
            cost   = sum(_cost_for(r.get("model", "unknown"), r.get("prompt_tokens", 0), r.get("completion_tokens", 0), _u_pricing) for r in recs)
            return tokens, cost

        _u_today_tok, _u_today_cost = _u_totals(_u_today_recs)
        _u_week_tok,  _u_week_cost  = _u_totals(_u_week_recs)
        _u_all_tok,   _u_all_cost   = _u_totals(_u_records)

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Today — cost",         f"${_u_today_cost:.4f}")
        c2.metric("Today — tokens",       f"{_u_today_tok:,}")
        c3.metric("Last 7 days — cost",   f"${_u_week_cost:.4f}")
        c4.metric("Last 7 days — tokens", f"{_u_week_tok:,}")
        c5.metric("Last 7 days — calls",  f"{len(_u_week_recs):,}")

        st.divider()
        st.markdown("**Daily cost — last 7 days**")
        _u_daily = _dd(float)
        for _d_off in range(6, -1, -1):
            _u_daily[(_u_today - _dt.timedelta(days=_d_off)).isoformat()] = 0.0
        for r in _u_week_recs:
            d = _u_record_date(r)
            if d:
                _u_daily[d.isoformat()] += _cost_for(
                    r.get("model", "unknown"), r.get("prompt_tokens", 0), r.get("completion_tokens", 0), _u_pricing
                )
        import pandas as _pd_usage
        _u_chart = _pd_usage.DataFrame({"Date": list(_u_daily.keys()), "Cost (USD)": list(_u_daily.values())}).set_index("Date")
        st.bar_chart(_u_chart)

        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("**Last 7 days — by model**")
            _u_by_model = _dd(lambda: {"calls": 0, "tokens": 0, "cost": 0.0})
            for r in _u_week_recs:
                m = r.get("model", "unknown")
                _u_by_model[m]["calls"]  += 1
                _u_by_model[m]["tokens"] += r.get("total_tokens", 0)
                _u_by_model[m]["cost"]   += _cost_for(m, r.get("prompt_tokens", 0), r.get("completion_tokens", 0), _u_pricing)
            st.dataframe(
                [{"Model": m, "Calls": s["calls"], "Tokens": s["tokens"], "Cost (USD)": round(s["cost"], 4)}
                 for m, s in sorted(_u_by_model.items())],
                use_container_width=True, hide_index=True,
            )
        with col_b:
            st.markdown("**Last 7 days — by call type**")
            _u_by_type = _dd(lambda: {"calls": 0, "tokens": 0, "cost": 0.0})
            for r in _u_week_recs:
                t = r.get("call_type", "unknown")
                _u_by_type[t]["calls"]  += 1
                _u_by_type[t]["tokens"] += r.get("total_tokens", 0)
                _u_by_type[t]["cost"]   += _cost_for(r.get("model", "unknown"), r.get("prompt_tokens", 0), r.get("completion_tokens", 0), _u_pricing)
            st.dataframe(
                [{"Call type": t, "Calls": s["calls"], "Tokens": s["tokens"], "Cost (USD)": round(s["cost"], 4)}
                 for t, s in sorted(_u_by_type.items())],
                use_container_width=True, hide_index=True,
            )

        st.divider()
        st.caption(f"All-time: {len(_u_records):,} AI calls · {_u_all_tok:,} tokens · **${_u_all_cost:.4f}** estimated cost.")

# TAB: Guide
# =============================================================================
with tab_guide:
    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown("""
    <div style="display:flex;align-items:center;gap:14px;padding:8px 0 16px;">
        <div style="width:44px;height:44px;border-radius:10px;
                    background:linear-gradient(135deg,#4F46E5,#818CF8);
                    display:flex;align-items:center;justify-content:center;font-size:1.3rem;">📘</div>
        <div>
            <div style="font-size:1.2rem;font-weight:800;color:#0F172A;">Documentation &amp; Guide</div>
            <div style="font-size:0.8rem;color:#64748B;">Everything you need to know — from quickstart to RBAC details</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Quick nav sub-tabs ────────────────────────────────────────────────────
    _g1, _g2, _g3, _g4 = st.tabs(["🚀 Quickstart", "⚙️ Features", "🔐 Security & RBAC", "📜 Audit & Compliance"])

    with _g1:
        # Workflow diagram
        st.markdown("""
        <div style="background:white;border:1px solid #E2E8F0;border-radius:12px;padding:20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.06);">
            <div style="font-size:0.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#94A3B8;margin-bottom:12px;">END-TO-END WORKFLOW</div>
            <div style="display:flex;align-items:center;gap:0;flex-wrap:wrap;">
                <div style="background:#EEF2FF;border:1.5px solid #A5B4FC;border-radius:999px;padding:7px 18px;font-size:0.8rem;font-weight:700;color:#4338CA;">1 · Connect</div>
                <div style="color:#A5B4FC;font-size:1.2rem;padding:0 6px;">→</div>
                <div style="background:#EEF2FF;border:1.5px solid #A5B4FC;border-radius:999px;padding:7px 18px;font-size:0.8rem;font-weight:700;color:#4338CA;">2 · Exclusions</div>
                <div style="color:#A5B4FC;font-size:1.2rem;padding:0 6px;">→</div>
                <div style="background:#EEF2FF;border:1.5px solid #A5B4FC;border-radius:999px;padding:7px 18px;font-size:0.8rem;font-weight:700;color:#4338CA;">3 · Generate YAML</div>
                <div style="color:#A5B4FC;font-size:1.2rem;padding:0 6px;">→</div>
                <div style="background:#EEF2FF;border:1.5px solid #A5B4FC;border-radius:999px;padding:7px 18px;font-size:0.8rem;font-weight:700;color:#4338CA;">4 · Review &amp; Approve</div>
                <div style="color:#A5B4FC;font-size:1.2rem;padding:0 6px;">→</div>
                <div style="background:#EEF2FF;border:1.5px solid #A5B4FC;border-radius:999px;padding:7px 18px;font-size:0.8rem;font-weight:700;color:#4338CA;">5 · Run Validation</div>
                <div style="color:#A5B4FC;font-size:1.2rem;padding:0 6px;">→</div>
                <div style="background:#ECFDF5;border:1.5px solid #6EE7B7;border-radius:999px;padding:7px 18px;font-size:0.8rem;font-weight:700;color:#065F46;">✅ Results</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Step cards
        _steps = [
            ("Configure connections", ".env holds your source DB and Snowflake credentials. No credentials are entered inside the app itself — it reads from the environment, keeping secrets out of session state."),
            ("Set exclusion policy", "Open the Exclusions tab and add any columns your team always wants skipped (Fivetran metadata, internal audit columns). These apply globally per source type."),
            ("Generate Single YAML", "Pick source table → Snowflake table. Preview the column mapping, fix any wrong suggestions in the grid, then click Generate. The YAML and SQL files are written to the chosen medallion layer folder."),
            ("Batch-generate for a schema", "Once you're confident one table works, switch to Batch YAML. Select many tables at once, map each to its Snowflake target, and generate in one pass."),
            ("Review & Approve", "Any mapping the AI is less than 95% confident about goes to PENDING. A human reviewer must approve, reject, or modify it before it can be used."),
            ("Run Validation", "Execute the Run Validation tab. It calls Project/main.py against the generated YAMLs and shows PASS/FAIL per table with a row-level diff view for failures."),
        ]
        _sc1, _sc2 = st.columns(2)
        for i, (title, body) in enumerate(_steps):
            col = _sc1 if i % 2 == 0 else _sc2
            col.markdown(f"""
            <div class="step-card" style="margin-bottom:22px;margin-top:18px;">
                <div class="step-num">{i+1}</div>
                <div class="step-title">{title}</div>
                <div class="step-body">{body}</div>
            </div>
            """, unsafe_allow_html=True)

        # What it does box
        st.markdown("""
        <div style="background:#F0FDF4;border:1px solid #86EFAC;border-radius:10px;padding:16px 20px;margin-top:8px;">
        <div style="font-weight:700;color:#166534;margin-bottom:8px;">What Migration Validator produces for every table</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:0.83rem;color:#14532D;">
            <div>📄 <b>Source SQL</b> — normalised SELECT query for the source DB (PostgreSQL / MSSQL / Athena)</div>
            <div>🏔️ <b>Snowflake SQL</b> — matching normalised SELECT for the Snowflake target</div>
            <div>📋 <b>Validation YAML</b> — config that ties both queries together with metadata</div>
            <div>📊 <b>Row-level CSV</b> — PASS/FAIL status per row with source vs. target values side-by-side</div>
        </div>
        </div>
        """, unsafe_allow_html=True)

    with _g2:
        # Column exclusions
        st.markdown("""
        <div style="font-size:1rem;font-weight:700;color:#0F172A;margin:4px 0 12px;">Column exclusion — 3 categories</div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:20px;">
            <div style="background:white;border:1px solid #E2E8F0;border-radius:10px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.06);">
                <div style="font-size:0.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#64748B;margin-bottom:6px;">🔒 Built-in</div>
                <div style="font-size:0.83rem;color:#334155;">Fivetran metadata columns hardcoded in the app. Always excluded — no UI to change them.</div>
            </div>
            <div style="background:white;border:1px solid #E2E8F0;border-radius:10px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.06);">
                <div style="font-size:0.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#64748B;margin-bottom:6px;">🌐 Global user exclusions</div>
                <div style="font-size:0.83rem;color:#334155;">Managed in the <b>Exclusions</b> tab. Apply to every table of that source type.</div>
            </div>
            <div style="background:white;border:1px solid #E2E8F0;border-radius:10px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.06);">
                <div style="font-size:0.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#64748B;margin-bottom:6px;">➕ Run-specific</div>
                <div style="font-size:0.83rem;color:#334155;">The picker on Single/Batch YAML. One-off skip for this generation only — not saved anywhere.</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Rule book
        st.markdown("---")
        st.markdown("""
        <div style="font-size:1rem;font-weight:700;color:#0F172A;margin:4px 0 12px;">Rule Book — how types are normalised</div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:20px;">
            <div style="background:#ECFDF5;border:1px solid #86EFAC;border-radius:10px;padding:14px;">
                <div style="font-weight:700;color:#065F46;margin-bottom:5px;">🔒 Base rules</div>
                <div style="font-size:0.82rem;color:#166534;">In <code>base_rules.py</code>. Always run first. Cannot be overridden.</div>
            </div>
            <div style="background:#EEF2FF;border:1px solid #C7D2FE;border-radius:10px;padding:14px;">
                <div style="font-weight:700;color:#3730A3;margin-bottom:5px;">📝 Draft rules</div>
                <div style="font-size:0.82rem;color:#4338CA;">Saved to <code>rule_book_learned.json</code>. Advisory only — never generate real SQL until activated.</div>
            </div>
            <div style="background:#FEF3C7;border:1px solid #FDE68A;border-radius:10px;padding:14px;">
                <div style="font-weight:700;color:#92400E;margin-bottom:5px;">⚡ Active rules</div>
                <div style="font-size:0.82rem;color:#78350F;">Gap fillers — only for type pairs with no base rule. Activate a draft in the Rule Book tab.</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Custom SQL
        st.markdown("---")
        st.markdown("""
        <div style="font-size:1rem;font-weight:700;color:#0F172A;margin:4px 0 8px;">Custom SQL from a prompt</div>
        <div style="font-size:0.85rem;color:#475569;line-height:1.65;">
        After previewing a column mapping in Single or Batch YAML, a <b>🧪 Generate custom SQL from a prompt</b>
        section appears. Use it when you need something the standard column-diff doesn't cover:
        <ul style="margin:8px 0 0 16px;">
            <li>Aggregate checks — <em>"Count rows where status = 'active', grouped by region"</em></li>
            <li>Filtered subsets — <em>"Compare only orders in the last 30 days"</em></li>
            <li>Lightweight sanity checks — <em>"Row count only, no column comparison"</em></li>
        </ul>
        Always review the generated SQL before trusting it — it's a starting point, not a guarantee.
        </div>
        """, unsafe_allow_html=True)

        # AI chat
        st.markdown("---")
        st.markdown("""
        <div style="font-size:1rem;font-weight:700;color:#0F172A;margin:4px 0 8px;">AI Migration Intelligence Chat</div>
        <div style="font-size:0.85rem;color:#475569;line-height:1.65;">
        The <b>✨ chat bubble</b> (bottom-right) gives you a governed tool loop — not a free chatbot:
        <ol style="margin:8px 0 0 16px;">
            <li>You type a natural-language request.</li>
            <li>The AI selects from <b>24 registered tools</b> and calls them in sequence (up to 10 rounds).</li>
            <li>Every tool returns live data — no fabrication.</li>
            <li>Results are synthesised into a plain-English response.</li>
            <li>Every tool call is visible in the <b>🔧 Tools used</b> expander below each reply.</li>
        </ol>
        <b>Quick actions</b> send pre-built prompts so you don't have to type.
        </div>
        """, unsafe_allow_html=True)

    with _g3:
        st.markdown("""
        <div style="font-size:1rem;font-weight:700;color:#0F172A;margin:4px 0 12px;">Authentication modes</div>
        """, unsafe_allow_html=True)
        import pandas as _pd_guide
        _auth_table = {
            "Mode": ["`jwt`", "`static`", "`dev`"],
            "When to use": ["Production / staging", "CI pipelines, demos", "Local development only"],
            "How it works": [
                "Validates a signed JWT (HS256 or RS256). Roles extracted from token claims. Expiry, issuer, and audience enforced.",
                "Pre-shared bearer token (CONNECTOR_API_TOKEN). Roles via CONNECTOR_ROLES env var.",
                "No validation. Every request accepted with ADMIN role. Never use outside localhost.",
            ],
            "Env vars": [
                "JWT_SECRET or JWT_PUBLIC_KEY; optionally JWT_ISSUER, JWT_AUDIENCE",
                "CONNECTOR_API_TOKEN; optionally CONNECTOR_ROLES",
                "None",
            ],
        }
        st.dataframe(_pd_guide.DataFrame(_auth_table), hide_index=True, use_container_width=True)

        st.markdown("---")
        st.markdown("""
        <div style="font-size:1rem;font-weight:700;color:#0F172A;margin:4px 0 12px;">Role-Based Access Control (RBAC)</div>
        <div style="font-size:0.83rem;color:#475569;margin-bottom:12px;">Roles are cumulative — each level includes all permissions of the level below it.</div>
        """, unsafe_allow_html=True)
        _rbac_rows = [
            ("VIEWER",               "#F1F5F9", "#334155", "Read schemas, mappings, rules, validation results"),
            ("REVIEWER",             "#EEF2FF", "#3730A3", "VIEWER + approve / reject / modify column mappings"),
            ("RULE_ADMIN",           "#FEF3C7", "#92400E", "REVIEWER + create, update, approve and activate transformation rules"),
            ("VALIDATION_OPERATOR",  "#ECFDF5", "#065F46", "REVIEWER + trigger validation runs, generate and execute SQL"),
            ("ADMIN",                "#FFF1F2", "#9F1239", "All permissions across all resources"),
        ]
        for role, bg, fg, desc in _rbac_rows:
            st.markdown(f"""
            <div style="background:{bg};border-radius:8px;padding:10px 16px;margin-bottom:6px;display:flex;align-items:center;gap:14px;">
                <div style="font-size:0.75rem;font-weight:800;color:{fg};font-family:monospace;min-width:140px;">{role}</div>
                <div style="font-size:0.82rem;color:#334155;">{desc}</div>
            </div>
            """, unsafe_allow_html=True)

        st.warning(
            "**Security invariant:** The string `gemini_ai` is always rejected as an actor on write tools. "
            "The AI agent can never self-approve — a human with the correct role must confirm.",
            icon="🛡️",
        )

    with _g4:
        # Confidence tiers
        st.markdown("""
        <div style="font-size:1rem;font-weight:700;color:#0F172A;margin:4px 0 8px;">Confidence tiers &amp; approval gates</div>
        """, unsafe_allow_html=True)
        _conf_tiers = [
            ("≥ 95%", "Auto-accepted", "#ECFDF5", "#065F46", "Proceeds to plan immediately — no human action required"),
            ("75–95%", "Pending review", "#FEF3C7", "#92400E", "Reviewer must approve or reject before mapping is used"),
            ("< 75%", "Mandatory review", "#FFF1F2", "#9F1239", "Reviewer must approve, reject, or modify — cannot skip"),
        ]
        for conf, state, bg, fg, desc in _conf_tiers:
            st.markdown(f"""
            <div style="background:{bg};border-radius:8px;padding:12px 16px;margin-bottom:8px;
                        display:flex;align-items:center;gap:16px;">
                <div style="font-size:1rem;font-weight:800;color:{fg};min-width:60px;text-align:center;">{conf}</div>
                <div>
                    <div style="font-weight:700;color:{fg};font-size:0.85rem;">{state}</div>
                    <div style="font-size:0.8rem;color:#334155;">{desc}</div>
                </div>
            </div>
            """, unsafe_allow_html=True)

        st.markdown("---")
        st.markdown("""
        <div style="font-size:1rem;font-weight:700;color:#0F172A;margin:4px 0 8px;">Audit trail</div>
        <div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;padding:16px 20px;font-size:0.83rem;color:#334155;">
        Every write action — approval, rejection, modification, rule activation, plan sign-off — is appended to
        an <b>immutable append-only JSONL log</b> at <code>output/audit_log.jsonl</code>. Records are never
        modified or deleted.<br><br>
        Each record contains: <code>audit_id</code> · <code>action</code> · <code>actor</code> ·
        <code>timestamp</code> (ISO 8601 UTC) · <code>resource_type</code> / <code>resource_id</code> ·
        <code>reason</code> · <code>before</code> / <code>after</code> state snapshot.<br><br>
        <b>Never recorded:</b> passwords, API keys, JWT secrets, or raw bearer tokens.
        </div>
        """, unsafe_allow_html=True)
        st.info(
            "The audit trail is designed for regulatory and compliance review. It can be exported to a SIEM "
            "or reviewed by a security team without exposing any credentials.",
            icon="📋",
        )

        st.markdown("---")
        st.markdown("""
        <div style="font-size:1rem;font-weight:700;color:#0F172A;margin:4px 0 8px;">Human-in-the-loop guarantee</div>
        <div style="font-size:0.83rem;color:#475569;line-height:1.65;">
        Three review decisions are available in the <b>✅ Review &amp; Approve</b> tab:
        <ul style="margin:8px 0 0 16px;">
            <li><b>Approve</b> — accepts the AI mapping as-is. Recorded with your identity and timestamp.</li>
            <li><b>Reject</b> — discards the mapping. It will not be used. Reason is required and recorded.</li>
            <li><b>Modify</b> — accepts the mapping but substitutes a different target column. Both the
                original AI suggestion and your override are recorded for auditability.</li>
        </ul>
        All decisions use <b>optimistic concurrency control (OCC)</b> — two reviewers cannot simultaneously
        approve the same mapping, eliminating race conditions.
        </div>
        """, unsafe_allow_html=True)

    # (old flat-markdown guide replaced by sub-tabs above)

# =============================================================================
# TAB: Output Files
# =============================================================================
with tab_files:
    import pathlib

    st.markdown("""
    <div style="display:flex;align-items:center;gap:14px;padding:8px 0 16px;">
        <div style="width:44px;height:44px;border-radius:10px;
                    background:linear-gradient(135deg,#0EA5E9,#38BDF8);
                    display:flex;align-items:center;justify-content:center;font-size:1.3rem;">📂</div>
        <div>
            <div style="font-size:1.2rem;font-weight:800;color:#0F172A;">Output Files</div>
            <div style="font-size:0.8rem;color:#64748B;">Browse CSV / Excel files from past validation runs — color-coded PASS / FAIL inline</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    _OUT_ROOT = pathlib.Path(__file__).parent.parent / "Project" / "output"

    # ── Discover files ────────────────────────────────────────────────────────
    _all_files = sorted(
        [p for p in _OUT_ROOT.rglob("*") if p.suffix in {".csv", ".xlsx"}],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ) if _OUT_ROOT.exists() else []

    if not _all_files:
        st.info("No CSV or Excel files found in `Project/output/`. Run a validation first.", icon="📭")
    else:
        # ── Sidebar-style filters in a card ──────────────────────────────────
        with st.container():
            _fc1, _fc2, _fc3 = st.columns([2, 2, 1])
            with _fc1:
                _layer_opts = sorted({p.parts[len(_OUT_ROOT.parts)] for p in _all_files if len(p.parts) > len(_OUT_ROOT.parts)})
                _layer_sel = st.multiselect("Filter by layer", _layer_opts, key="of_layer")
            with _fc2:
                _ext_sel = st.multiselect("File type", [".csv", ".xlsx"], default=[".csv", ".xlsx"], key="of_ext")
            with _fc3:
                _refresh = st.button("🔄 Refresh", use_container_width=True, key="of_refresh")

        _filtered = [
            p for p in _all_files
            if p.suffix in (_ext_sel or {".csv", ".xlsx"})
            and (not _layer_sel or (len(p.parts) > len(_OUT_ROOT.parts) and p.parts[len(_OUT_ROOT.parts)] in _layer_sel))
        ]

        if not _filtered:
            st.warning("No files match the current filters.")
        else:
            # ── Summary metrics ───────────────────────────────────────────────
            _m1, _m2, _m3 = st.columns(3)
            _m1.metric("Total files", len(_filtered))
            _m2.metric("CSV", sum(1 for p in _filtered if p.suffix == ".csv"))
            _m3.metric("Excel", sum(1 for p in _filtered if p.suffix == ".xlsx"))

            st.divider()

            # ── File picker (relative paths for readability) ──────────────────
            _rel_labels = {str(p.relative_to(_OUT_ROOT)): p for p in _filtered}
            _selected_label = st.selectbox(
                "Select file",
                list(_rel_labels.keys()),
                key="of_picker",
                help="Files sorted newest first",
            )
            _selected_path = _rel_labels[_selected_label]

            # ── File info bar ─────────────────────────────────────────────────
            import os, datetime as _dt
            _mtime = _dt.datetime.fromtimestamp(_selected_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            _fsize = _selected_path.stat().st_size
            _size_str = f"{_fsize / 1024:.1f} KB" if _fsize < 1_048_576 else f"{_fsize / 1_048_576:.1f} MB"
            _ic1, _ic2, _ic3 = st.columns(3)
            _ic1.caption(f"**Modified:** {_mtime}")
            _ic2.caption(f"**Size:** {_size_str}")
            _ic3.caption(f"**Type:** {_selected_path.suffix.upper()[1:]}")

            # ── Load & render ─────────────────────────────────────────────────
            try:
                import pandas as pd
                if _selected_path.suffix == ".csv":
                    _df = pd.read_csv(_selected_path)
                else:
                    _df = pd.read_excel(_selected_path)

                # Route: diff-style CSVs (have __source/__target cols) use rich renderer;
                # summary CSVs use paginated styled table.
                _is_diff = any(c.endswith("__source") for c in _df.columns)

                if _is_diff:
                    _render_diff_file(_selected_path, key_prefix=f"of_{_selected_label}")
                else:
                    _tab_view, _tab_raw = st.tabs(["📊 Styled view", "🗃 Raw data"])
                    with _tab_view:
                        render_paginated_df(_df, key_prefix=f"of_pg_{_selected_label}")
                    with _tab_raw:
                        st.dataframe(_df, use_container_width=True, hide_index=True)

                # ── Download button ───────────────────────────────────────────
                with open(_selected_path, "rb") as _fh:
                    st.download_button(
                        label=f"⬇️ Download {_selected_path.name}",
                        data=_fh.read(),
                        file_name=_selected_path.name,
                        mime="text/csv" if _selected_path.suffix == ".csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="of_dl",
                    )
            except Exception as _e:
                st.error(f"Could not load file: {_e}")
