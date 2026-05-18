import json
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import text

import config_paths
from database_utils import get_engine
from logger_config import setup_logging

load_dotenv()
logger = setup_logging()

MANIFEST_KEY_S3 = "bronze/manifests/_process_manifest_clients.json"


# ── MANIFEST ──────────────────────────────────────────────────────────────────

def _load_manifest() -> list:
    try:
        return config_paths.read_json_from_s3(MANIFEST_KEY_S3)
    except Exception:
        return []


def _save_manifest(tasks: list) -> None:
    config_paths.write_json_to_s3(tasks, MANIFEST_KEY_S3)


# ── EXTRACT ───────────────────────────────────────────────────────────────────

def extract_clients_from_json(file_path: str) -> pd.DataFrame:
    try:
        raw         = config_paths.read_json_from_s3(file_path)
        source_name = file_path.split("/")[-1]
        df = pd.DataFrame(raw)
        df["_ingested_at_utc"] = datetime.now(timezone.utc)
        df["_source_file"]     = source_name
        logger.debug("[EXTRACT] %d filas leídas de %s", len(df), source_name)
        return df
    except Exception as exc:
        logger.error("[EXTRACT] Error leyendo %s: %s", file_path, exc)
        return pd.DataFrame()


# ── TRANSFORM ─────────────────────────────────────────────────────────────────

def transform_clients_bronze_to_silver(df_raw: pd.DataFrame) -> pd.DataFrame:
    if df_raw.empty:
        logger.warning("[TRANSFORM] DataFrame vacío — nada que transformar")
        return pd.DataFrame()

    logger.info("[TRANSFORM] Transformando %d registro(s) raw", len(df_raw))
    try:
        df = df_raw.copy()

        numeric_cols = [
            "latitude", "longitude", "nominal_load_kw", "pv_peak_power_kw",
            "panel_area_m2", "efficiency", "loss_pct", "angle", "aspect",
            "battery_capacity_kwh", "soc_min_pct", "installation_cost_eur",
        ]
        text_cols = [
            "client_id", "name", "description", "panel_type",
            "mounting", "timezone", "_ingested_at_utc",
        ]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        for col in text_cols:
            df[col] = df[col].astype(str).replace(["None", "nan", "NaN", "null"], np.nan)

        df["_ingested_at_utc"] = pd.to_datetime(df["_ingested_at_utc"], errors="coerce")

        critical = ["client_id", "name", "latitude", "longitude", "pv_peak_power_kw", "_ingested_at_utc"]
        before   = len(df)
        df       = df.dropna(subset=critical)
        if before - len(df):
            logger.warning("[TRANSFORM] %d registro(s) descartados por campos críticos nulos", before - len(df))

        df["latitude"]  = df["latitude"].round(6)
        df["longitude"] = df["longitude"].round(6)
        df["name"]      = df["name"].str.upper().str.strip()

        df = df[df["latitude"].between(-90, 90) & df["longitude"].between(-180, 180)]
        df.loc[~df["angle"].between(0, 90),       "angle"]       = 30.0
        df.loc[~df["aspect"].between(1, 360),     "aspect"]      = 180.0
        df.loc[~df["loss_pct"].between(0, 90),    "loss_pct"]    = 14.0
        df.loc[~df["soc_min_pct"].between(0, 90), "soc_min_pct"] = 20.0
        df.loc[df["efficiency"].notna() & ~df["efficiency"].between(0, 1), "efficiency"] = 0.15
        df = df[df["pv_peak_power_kw"] > 0]
        for col in ["panel_area_m2", "battery_capacity_kwh", "installation_cost_eur"]:
            df.loc[df[col] < 0, col] = 0

        df = df.sort_values("_ingested_at_utc", ascending=False).drop_duplicates(
            subset=["client_id"], keep="first"
        )
        df = df.fillna({
            "description":           "unknown",
            "nominal_load_kw":       df["pv_peak_power_kw"] * 1.3,
            "panel_area_m2":         0.0,
            "efficiency":            0.15,
            "panel_type":            "unknown",
            "loss_pct":              14.0,
            "angle":                 30.0,
            "aspect":                180.0,
            "mounting":              "unknown",
            "battery_capacity_kwh":  0.0,
            "soc_min_pct":           20.0,
            "installation_cost_eur": 0.0,
            "timezone":              "UTC",
        })

        df = df.reset_index(drop=True)
        logger.info("[TRANSFORM] Registros Silver producidos: %d", len(df))
        return df

    except Exception as exc:
        logger.error("[TRANSFORM] Transformación fallida: %s", exc)
        return pd.DataFrame()


# ── LOAD → SILVER ─────────────────────────────────────────────────────────────

def load_clients_to_silver(df: pd.DataFrame, table_name: str = "clean_clients") -> bool:
    engine = get_engine()
    if engine is None:
        return False
    if df.empty:
        return False

    logger.info("[LOAD] Escribiendo %d registro(s) en '%s'", len(df), table_name)
    try:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
            conn.execute(text(f"""
                CREATE TABLE {table_name} (
                    client_id               TEXT NOT NULL PRIMARY KEY,
                    name                    TEXT NOT NULL,
                    description             TEXT NOT NULL,
                    latitude                REAL NOT NULL,
                    longitude               REAL NOT NULL,
                    nominal_load_kw         REAL NOT NULL,
                    pv_peak_power_kw        REAL NOT NULL,
                    panel_area_m2           REAL NOT NULL,
                    efficiency              REAL NOT NULL,
                    panel_type              TEXT NOT NULL,
                    loss_pct                REAL NOT NULL,
                    angle                   REAL NOT NULL,
                    aspect                  REAL NOT NULL,
                    mounting                TEXT NOT NULL,
                    battery_capacity_kwh    REAL NOT NULL,
                    soc_min_pct             REAL NOT NULL,
                    installation_cost_eur   REAL NOT NULL,
                    timezone                TEXT NOT NULL,
                    _source_file            TEXT NOT NULL,
                    _ingested_at_utc        TEXT NOT NULL
                )
            """))
            df.to_sql(table_name, con=conn, if_exists="append", index=False)
        logger.info("[LOAD] '%s' reconstruida — %d registro(s)", table_name, len(df))
        return True
    except Exception as exc:
        logger.error("[LOAD] Error escribiendo '%s': %s", table_name, exc)
        return False


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def transform_clients() -> int:
    logger.info("[INIT] ── transform_clients starting ──────────────────────────")

    all_tasks  = _load_manifest()
    actionable = [t for t in all_tasks if t["status"] in ("pending", "error")]

    if not actionable:
        logger.info("[INIT] Todas las tareas ya procesadas")
        return 0

    session_rows = session_ok = session_err = 0

    for task in actionable:
        path_file = task["path"]
        fname     = path_file.split("/")[-1]
        try:
            df_raw = extract_clients_from_json(path_file)
            if df_raw.empty:
                raise ValueError("Bronze file vacío o ilegible")
            df_silver = transform_clients_bronze_to_silver(df_raw)
            if df_silver.empty:
                raise ValueError("Transformación produjo DataFrame vacío")
            rows = len(df_silver)
            if load_clients_to_silver(df_silver):
                task.update({"status": "success", "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
                task.pop("error", None)
                session_rows += rows
                session_ok   += 1
            else:
                raise ValueError("Silver load devolvió False")
        except Exception as exc:
            task.update({"status": "error", "error": str(exc),
                         "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
            session_err += 1
            logger.error("[ERROR] %s: %s", fname, exc)

    _save_manifest(all_tasks)
    logger.info("[DONE] transform_clients — ok: %d | errores: %d | filas: %d",
                session_ok, session_err, session_rows)
    return session_rows


if __name__ == "__main__":
    transform_clients()