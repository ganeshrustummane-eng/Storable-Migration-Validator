from datetime import datetime
from pathlib import Path
import os
import pandas as pd
import logging
import sys


#Generating runids
def generate_runid():
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f"),datetime.now().strftime("%d-%b-%Y %H:%M:%S.%f")


def count_validation_match(source_rows: int, target_rows: int, threshold_pct: float = 0):
    """Decide PASS/FAIL for count_validation. threshold_pct=0 means exact match
    required (the historical default — every existing YAML keeps this behavior
    unless it explicitly sets count_mismatch_threshold_pct). Returns (is_match, diff_pct)."""
    if threshold_pct > 0 and source_rows > 0:
        diff_pct = abs(target_rows - source_rows) / source_rows * 100
        return diff_pct <= threshold_pct, diff_pct
    return source_rows == target_rows, (0.0 if source_rows == target_rows else float("inf"))


def row_hash_fallback_looks_like_column_drift(n_source_only: int, n_target_only: int, total_rows: int) -> bool:
    """Heuristic for the row_hash PK fallback: when no primary key is configured,
    the row's identity IS the hash of every common column, so one un-normalized
    column (e.g. timestamp precision) desyncs every hash on both sides — every
    row looks "missing" from the other side's index instead of one clear
    column-level mismatch. Roughly-equal SOURCE_ONLY/TARGET_ONLY counts across
    most of the table is that failure mode's signature, not real row loss."""
    if total_rows <= 0 or n_source_only == 0 or n_target_only == 0:
        return False
    closeness = 1 - abs(n_source_only - n_target_only) / max(n_source_only, n_target_only)
    return closeness >= 0.9 and (n_source_only + n_target_only) / total_rows >= 0.5


#Function to read the correct configuration file based on the parameters passed
def get_config_output_paths(run_id,layer_type,base_dir,config_path,validation_dirs,table_list):
    outputpaths = {}
    configpaths = {}


    output_dir = os.path.join(base_dir, "output")
    logpath = os.path.join(
        output_dir,
        layer_type[0],
        f"validation_{run_id}"
    )
    os.makedirs(output_dir, exist_ok=True)
    #Creating directories based on parameters passed
    for validation in validation_dirs:
        if validation == 'count_validation':
            path = os.path.join(
                output_dir,
                layer_type[0],
                f"validation_{run_id}",
                f"{validation}_{run_id}"
            )

            cv_dir = Path(config_path) / layer_type[0] / validation
            yaml_files = sorted(cv_dir.glob("*.yaml")) if cv_dir.exists() else []
            # Fall back to legacy {layer}.yaml name if no source-segregated files exist yet
            if not yaml_files:
                legacy = cv_dir / f"{layer_type[0]}.yaml"
                yaml_files = [legacy] if legacy.exists() else []
            yamlpaths = [str(p) for p in yaml_files]

            os.makedirs(path, exist_ok=True)
            outputpaths[validation] = path
            configpaths[validation] = yamlpaths

        if validation == 'data_validation':
            path = os.path.join(
                output_dir,
                layer_type[0],
                f"validation_{run_id}",
                f"{validation}_{run_id}" 
            )

            yaml_paths = []
            config_root = Path(base_dir) / "config" / layer_type[0]
            report_root = Path(base_dir) / "config" / "report"
            search_roots = [config_root] + ([report_root] if report_root.exists() else [])
            # Match both flat (data_validation/table.yaml) and subdir (data_validation/mssql/table.yaml)
            def _is_dv_yaml(p):
                return validation in (part for part in p.parts)
            if 'all' in table_list:
                yaml_paths = [str(p) for root in search_roots
                              for p in root.rglob("*.yaml") if _is_dv_yaml(p)]
            else:
                all_valid_yamls = {p.stem: str(p) for root in search_roots
                                   for p in root.rglob("*.yaml") if _is_dv_yaml(p)}
                for table in table_list:
                    if table in all_valid_yamls:
                        yaml_paths.append(all_valid_yamls[table])
                    else:
                        yaml_paths.append(str(config_root / validation / f"{table}.yaml"))

            configpaths[validation] = yaml_paths
            outputpaths[validation] = path
            
            os.makedirs(path, exist_ok=True)

    return outputpaths,configpaths,logpath

def create_summary(run_at,run_id,validation_type,source_table_name,source_type,target_table_name,target_type,source_rows,target_rows,output_file_path,output_path,status,batch_start_time,batch_end_time,diff_batch,missing_in_source=0,missing_in_target=0):
    if validation_type != 'count_validation':
        summary_df = pd.DataFrame([{
            "run_id": run_id,
            "run_at": run_at,
            "validation_performed": validation_type,
            "source_table_name": source_table_name,
            "source_type": source_type,
            "target_table_name": target_table_name,
            "target_type": target_type,
            "source_count": source_rows,
            "missing_in_source": missing_in_source,
            "target_count": target_rows,
            "missing_in_target": missing_in_target,
            "status": status,
            "output_file_path":output_file_path,
            "batch_start_time": batch_start_time, 
            "batch_end_time": batch_end_time,
            "total_time_taken": diff_batch
        }])
    else:

        summary_df = pd.DataFrame([{
            "run_id": run_id,
            "run_at": run_at,
            "validation_performed": validation_type,
            "source_table_name": source_table_name,
            "source_type": source_type,
            "target_table_name": target_table_name,
            "target_type": target_type,
            "source_count": source_rows ,
            "target_count": target_rows,
            "count_difference": abs(int(target_rows)-(int(source_rows))),
            "status": status,
            "batch_start_time": batch_start_time,
            "batch_end_time": batch_end_time,
            "total_time_taken": diff_batch
        }])

    summary_file = os.path.join(output_path,f"{validation_type}_summary.csv")

    summary_df.to_csv(summary_file,
        mode="a",
        index=False,
        header=not os.path.exists(summary_file))

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(filename)s:%(lineno)d | %(message)s"
    )

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)

    logger.addHandler(console_handler)

    return logger

def add_file_handler(
    logger: logging.Logger,
    log_directory: str,
    log_filename:str ="validation.log") -> logging.Logger:

    log_file = Path(os.path.join(log_directory,log_filename))

    # Prevent the same file handler from being added more than once.
    existing_files = {
        Path(handler.baseFilename).resolve()
        for handler in logger.handlers
        if isinstance(handler, logging.FileHandler)
    }

    if log_file.resolve() in existing_files:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | "
        "%(filename)s:%(lineno)d | %(message)s"
    )

    file_handler = logging.FileHandler(
        filename=log_file,
        mode="a",
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(file_handler)

    logger.info("Log file location: %s", log_file.resolve())

    return logger