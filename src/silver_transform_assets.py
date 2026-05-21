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

MANIFEST_KEY_S3 = "bronze/manifests/_process_manifest_assets.json"

VALID_ASSET_TYPES = {
    "forklift_battery", "compressor", "cold_storage",
    "pump", "autoclave", "lighting", "other",
}


# ── MANIFEST ──────────────────────────────────────────────────────────────────

def _load_manifest() -> list:
    try:
        return config_paths.read_json_from_s3(MANIFEST_KEY_S3)
    except Exception:
        return []


def _save_manifest(tasks: list) -> None:
    config_paths.write_json_to_s3(tasks, MANIFEST_KEY_S3)


# ── EXTRACT ───────────────────────────────────────────────────────────────────

def extract_assets_from_json(file_path: str) -> pd.DataFrame:
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

def transform_assets_bronze_to_silver(df_raw: pd.DataFrame) -> pd.DataFrame:
    if df_raw.empty:
        logger.warning("[TRANSFORM] DataFrame vacío — nada que transformar")
        return pd.DataFrame()

    logger.info("[TRANSFORM] Transformando %d registro(s) raw", len(df_raw))
    try:
        df = df_raw.copy()

        numeric_cols = [
            "power_kw", "capacity_kwh", "is_flexible",
            "flex_window_start", "flex_window_end", "priority",
        ]
        text_cols = ["client_id", "asset_id", "asset_name", "asset_type", "notes"]

        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        for col in text_cols:
            df[col] = df[col].astype(str).replace(["None", "nan", "NaN", "null"], np.nan)

        df["_ingested_at_utc"] = pd.to_datetime(df["_ingested_at_utc"], errors="coerce")

        # Críticos — sin estos no tiene sentido el registro
        critical = ["client_id", "asset_id", "asset_name", "asset_type", "power_kw"]
        before = len(df)
        df = df.dropna(subset=critical)
        dropped = before - len(df)
        if dropped:
            logger.warning("[TRANSFORM] %d registro(s) descartados por campos críticos nulos", dropped)

        # asset_type: normalizar a minúsculas y sustituir desconocidos por 'other'
        df["asset_type"] = df["asset_type"].str.strip().str.lower()
        invalid_mask = ~df["asset_type"].isin(VALID_ASSET_TYPES)
        if invalid_mask.any():
            logger.warning(
                "[TRANSFORM] %d asset_type(s) no reconocidos → sustituidos por 'other': %s",
                invalid_mask.sum(),
                df.loc[invalid_mask, "asset_type"].unique().tolist(),
            )
            df.loc[invalid_mask, "asset_type"] = "other"

        # Texto: strip y upper para IDs, title para nombres
        df["client_id"]   = df["client_id"].str.strip().str.upper()
        df["asset_id"]    = df["asset_id"].str.strip().str.upper()
        df["asset_name"]  = df["asset_name"].str.strip()
        df["notes"]       = df["notes"].fillna("").str.strip()

        # Rangos y valores razonables
        df["power_kw"]          = df["power_kw"].clip(lower=0)
        df["capacity_kwh"]      = df["capacity_kwh"].clip(lower=0)
        df["is_flexible"]       = df["is_flexible"].fillna(0).astype(int).clip(0, 1)
        df["priority"]          = df["priority"].fillna(99).astype(int).clip(1, 99)

        # Ventanas horarias: enteros 0–23
        df["flex_window_start"] = (
            df["flex_window_start"].fillna(0).astype(int).clip(0, 23)
        )
        df["flex_window_end"] = (
            df["flex_window_end"].fillna(23).astype(int).clip(0, 23)
        )

        # Coherencia ventana: start debe ser < end
        bad_window = df["flex_window_start"] >= df["flex_window_end"]
        if bad_window.any():
            logger.warning(
                "[TRANSFORM] %d registro(s) con flex_window_start >= flex_window_end "
                "— se resetean a 0–23",
                bad_window.sum(),
            )
            df.loc[bad_window, "flex_window_start"] = 0
            df.loc[bad_window, "flex_window_end"]   = 23

        # Deduplicar: si un asset_id aparece varias veces, quedarse con el más reciente
        df = df.sort_values("_ingested_at_utc", ascending=False).drop_duplicates(
            subset=["asset_id"], keep="first"
        )

        df = df.reset_index(drop=True)
        logger.info("[TRANSFORM] Registros Silver producidos: %d", len(df))
        return df

    except Exception as exc:
        logger.error("[TRANSFORM] Transformación fallida: %s", exc)
        return pd.DataFrame()


# ── LOAD → SILVER ─────────────────────────────────────────────────────────────

def load_assets_to_silver(df: pd.DataFrame, table_name: str = "clean_assets") -> bool:
    engine = get_engine()
    if engine is None:
        return False
    if df.empty:
        return False

    schema     = "silver"
    full_table = f"{schema}.{table_name}"
    logger.info("[LOAD] Escribiendo %d registro(s) en '%s'", len(df), full_table)

    try:
        df["_ingested_at_utc"] = pd.to_datetime(df["_ingested_at_utc"]).dt.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        with engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
            conn.execute(text(f"DROP TABLE IF EXISTS {full_table}"))
            conn.execute(text(f"""
                CREATE TABLE {full_table} (
                    asset_id            TEXT    NOT NULL PRIMARY KEY,
                    client_id           TEXT    NOT NULL,
                    asset_name          TEXT    NOT NULL,
                    asset_type          TEXT    NOT NULL,
                    power_kw            REAL    NOT NULL,
                    capacity_kwh        REAL    NOT NULL DEFAULT 0,
                    is_flexible         INTEGER NOT NULL DEFAULT 0,
                    flex_window_start   INTEGER NOT NULL DEFAULT 0,
                    flex_window_end     INTEGER NOT NULL DEFAULT 23,
                    priority            INTEGER NOT NULL DEFAULT 99,
                    notes               TEXT    NOT NULL DEFAULT '',
                    _source_file        TEXT    NOT NULL,
                    _ingested_at_utc    TIMESTAMP WITH TIME ZONE
                )
            """))
            df[[
                "asset_id", "client_id", "asset_name", "asset_type",
                "power_kw", "capacity_kwh", "is_flexible",
                "flex_window_start", "flex_window_end", "priority", "notes",
                "_source_file", "_ingested_at_utc",
            ]].to_sql(table_name, con=conn, if_exists="append", index=False, schema=schema)

        logger.info("[LOAD] '%s' reconstruida — %d registro(s)", full_table, len(df))
        return True

    except Exception as exc:
        logger.error("[LOAD] Error escribiendo '%s': %s", full_table, exc)
        return False


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def transform_assets() -> int:
    logger.info("[INIT] ── transform_assets starting ──────────────────────────")

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
            df_raw = extract_assets_from_json(path_file)
            if df_raw.empty:
                raise ValueError("Bronze file vacío o ilegible")

            df_silver = transform_assets_bronze_to_silver(df_raw)
            if df_silver.empty:
                raise ValueError("Transformación produjo DataFrame vacío")

            rows = len(df_silver)
            if load_assets_to_silver(df_silver):
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
    logger.info(
        "[DONE] transform_assets — ok: %d | errores: %d | filas: %d",
        session_ok, session_err, session_rows,
    )
    return session_rows


if __name__ == "__main__":
    transform_assets()