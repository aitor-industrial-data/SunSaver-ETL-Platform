import json
import os
import logging
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import text

import config_paths
from database_utils import get_engine
from logger_config import setup_logging

load_dotenv()
logger = setup_logging()

MANIFEST_KEY_S3 = f"{config_paths.get_bronze_prefix()}manifests/_process_manifest_esios_context_d1.json"


# ── MANIFEST ──────────────────────────────────────────────────────────────────

def _load_manifest() -> list:
    try:
        return config_paths.read_json_from_s3(MANIFEST_KEY_S3)
    except Exception:
        return []


def _save_manifest(tasks: list) -> None:
    config_paths.write_json_to_s3(tasks, MANIFEST_KEY_S3)


# ── EXTRACT ───────────────────────────────────────────────────────────────────

def extract_raw_context_from_json(file_path: str) -> pd.DataFrame:
    try:
        raw         = config_paths.read_json_from_s3(file_path)
        source_name = file_path.split("/")[-1]
        return pd.DataFrame([{
            "_ingested_at_utc": datetime.now(timezone.utc),
            "_source_file":     source_name,
            "raw_data":         json.dumps(raw),
        }])
    except Exception as exc:
        logger.error("[EXTRACT] Error leyendo %s: %s", file_path, exc)
        return pd.DataFrame()


# ── TRANSFORM ─────────────────────────────────────────────────────────────────

def transform_context_bronze_to_silver(df_raw: pd.DataFrame) -> pd.DataFrame:
    if df_raw.empty:
        return pd.DataFrame()

    logger.info("[TRANSFORM] Parseando y limpiando contexto ESIOS con límites de rango")

    VALID_RANGES = {
        "demand_real": {"min": 5000.0,   "max": 600000.0},   # MW
        "pv_gen":      {"min": 0.0,      "max": 500000.0},   # MW
        "co2":         {"min": 0.0,      "max": 100000.0},   # tCO2/h
        "upward_imb":  {"min": -50000.0, "max": 50000.0},    # Admite desvíos negativos
    }

    DEFAULT_RANGE = {"min": -100000.0, "max": 1000000.0}

    try:
        records = []
        for _, row in df_raw.iterrows():
            raw_json   = json.loads(row["raw_data"])
            indicators = raw_json.get("indicators", {})

            for ind_key, ind_data in indicators.items():
                indicator_meta = ind_data.get("indicator", {})
                indicator_id   = indicator_meta.get("id")
                limits         = VALID_RANGES.get(ind_key, DEFAULT_RANGE)

                for v in indicator_meta.get("values", []):
                    if v.get("geo_id") != 8741:
                        continue

                    val_raw = v.get("value")
                    if val_raw is None:
                        continue

                    try:
                        val_float = float(val_raw)
                        if not (limits["min"] <= val_float <= limits["max"]):
                            logger.warning(
                                "[VALIDATION] Valor fuera de rango omitido: "
                                "%s = %s en %s", ind_key, val_float, v.get("datetime_utc")
                            )
                            continue
                    except (ValueError, TypeError):
                        continue

                    records.append({
                        "indicator_name":   ind_key,
                        "indicator_id":     indicator_id,
                        "datetime_utc":     v.get("datetime_utc"),
                        "value":            val_float,
                        "_source_file":     row["_source_file"],
                        "_ingested_at_utc": row["_ingested_at_utc"],
                    })

        df = pd.DataFrame(records)
        if df.empty:
            return pd.DataFrame()

        df["datetime_utc"] = pd.to_datetime(df["datetime_utc"], errors="coerce", utc=True)
        df = df.dropna(subset=["datetime_utc"])

        df = (
            df.sort_values("_ingested_at_utc", ascending=False)
              .drop_duplicates(subset=["indicator_name", "datetime_utc"], keep="first")
              .sort_values(["indicator_name", "datetime_utc"])
              .reset_index(drop=True)
        )

        df["unix_time"] = df["datetime_utc"].astype("int64") // 10**9

        return df

    except Exception as exc:
        logger.error("[TRANSFORM] Transformación fallida: %s", exc)
        return pd.DataFrame()


# ── LOAD → SILVER ─────────────────────────────────────────────────────────────

def load_context_to_silver(df: pd.DataFrame, table_name: str = "clean_context") -> bool:
    engine = get_engine()
    if engine is None:
        return False
    if df.empty:
        logger.warning("[LOAD] El DataFrame de contexto está vacío. Nada que cargar.")
        return False

    logger.info("[LOAD] Upsertando %d registro(s) en PostgreSQL -> '%s'", len(df), table_name)
    try:
        df_sql = df.copy()
        df_sql["datetime_utc"]     = pd.to_datetime(df_sql["datetime_utc"], utc=True)
        df_sql["_ingested_at_utc"] = pd.to_datetime(df_sql["_ingested_at_utc"], utc=True)

        with engine.begin() as conn:
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {table_name} (
                    unix_time           BIGINT NOT NULL,
                    datetime_utc        TIMESTAMP WITH TIME ZONE NOT NULL,
                    indicator_name      VARCHAR(100) NOT NULL,
                    indicator_id        INTEGER,
                    value               DOUBLE PRECISION NOT NULL,
                    _source_file        VARCHAR(255),
                    _ingested_at_utc    TIMESTAMP WITH TIME ZONE NOT NULL,
                    PRIMARY KEY (unix_time, datetime_utc, indicator_name)
                )
            """))

            cols           = df_sql.columns.tolist()
            pk_cols        = ["unix_time", "datetime_utc", "indicator_name"]
            cols_to_update = [c for c in cols if c not in pk_cols]
            update_stmt    = ", ".join([f"{c} = EXCLUDED.{c}" for c in cols_to_update])

            conn.execute(text(f"""
                INSERT INTO {table_name} ({', '.join(cols)})
                VALUES ({', '.join(':' + c for c in cols)})
                ON CONFLICT (unix_time, datetime_utc, indicator_name)
                DO UPDATE SET {update_stmt}
            """), df_sql.to_dict(orient="records"))

        logger.info("[LOAD] '%s' actualizada — %d registro(s)", table_name, len(df))
        return True

    except Exception as exc:
        logger.error("[LOAD] Error escribiendo en Postgres ('%s'): %s", table_name, exc)
        return False


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def transform_energy_context() -> int:
    logger.info("[INIT] ── transform_energy_context starting ──────────────────")

    all_tasks  = _load_manifest()
    actionable = [t for t in all_tasks if t["status"] in ("pending", "error")]

    if not actionable:
        logger.info("[INIT] Todas las tareas de ESIOS context ya procesadas")
        return 0

    session_rows = session_ok = session_err = 0

    for task in actionable:
        path_file = task["path"]
        fname     = path_file.split("/")[-1]
        try:
            df_raw = extract_raw_context_from_json(path_file)
            if df_raw.empty:
                raise ValueError("Bronze file vacío o ilegible")

            df_silver = transform_context_bronze_to_silver(df_raw)
            if df_silver.empty:
                raise ValueError("Transformación produjo DataFrame vacío")

            rows = len(df_silver)
            if load_context_to_silver(df_silver):
                task.update({
                    "status":     "success",
                    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                })
                task.pop("error", None)
                session_rows += rows
                session_ok   += 1
            else:
                raise ValueError("Silver load devolvió False")

        except Exception as exc:
            task.update({
                "status":     "error",
                "error":      str(exc),
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            })
            session_err += 1
            logger.error("[ERROR] %s: %s", fname, exc)

    _save_manifest(all_tasks)
    logger.info("[DONE] transform_energy_context — ok: %d | errores: %d | filas: %d",
                session_ok, session_err, session_rows)
    return session_rows


if __name__ == "__main__":
    transform_energy_context()