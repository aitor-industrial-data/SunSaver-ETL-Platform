import json
import os
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import text

import config_paths
from database_utils import get_engine
from logger_config import setup_logging

load_dotenv()
logger = setup_logging()

MANIFEST_KEY_S3 = "bronze/manifests/_process_manifest_ree.json"


# ── MANIFEST ──────────────────────────────────────────────────────────────────

def _load_manifest() -> list:
    try:
        return config_paths.read_json_from_s3(MANIFEST_KEY_S3)
    except Exception:
        return []


def _save_manifest(tasks: list) -> None:
    config_paths.write_json_to_s3(tasks, MANIFEST_KEY_S3)


# ── EXTRACT ───────────────────────────────────────────────────────────────────

def extract_raw_ree_from_json(file_path: str) -> pd.DataFrame:
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

def transform_prices_bronze_to_silver(df_raw: pd.DataFrame) -> pd.DataFrame:
    if df_raw.empty:
        return pd.DataFrame()

    logger.info("[TRANSFORM] Parseando y limpiando precios REE")
    try:
        records = []
        for _, row in df_raw.iterrows():
            raw_json = json.loads(row["raw_data"])
            for series in raw_json.get("included", []):
                price_type = series.get("type")
                for v in series.get("attributes", {}).get("values", []):
                    records.append({
                        "price_type":       price_type,
                        "datetime_utc":     v.get("datetime"),
                        "price_euro_mwh":   float(v.get("value")),
                        "_source_file":     row["_source_file"],
                        "_ingested_at_utc": row["_ingested_at_utc"],
                    })

        df = pd.DataFrame(records)
        if df.empty:
            return pd.DataFrame()

        df["datetime_utc"] = pd.to_datetime(df["datetime_utc"], errors="coerce", utc=True)
        df = df.dropna(subset=["datetime_utc"])

        outlier_mask = (df["price_euro_mwh"] < -100) | (df["price_euro_mwh"] > 2000)
        if outlier_mask.sum():
            logger.warning("[TRANSFORM] %d outlier(s) filtrados", outlier_mask.sum())
            df = df[~outlier_mask]

        df = (
            df.sort_values("_ingested_at_utc", ascending=False)
              .drop_duplicates(subset=["price_type", "datetime_utc"], keep="first")
              .sort_values(["price_type", "datetime_utc"])
              .reset_index(drop=True)
        )

        df["price_euro_mwh"] = df.groupby("price_type")["price_euro_mwh"].transform(
            lambda x: x.interpolate(method="linear").ffill().bfill().round(4)
        )

        # unix_time calculado directamente desde datetime_utc tz-aware, sin quitar timezone
        df["unix_time"] = df["datetime_utc"].astype("int64") // 10**9

        logger.info("[TRANSFORM] %d registros Silver de precios producidos", len(df))
        return df

    except Exception as exc:
        logger.error("[TRANSFORM] Transformación de precios fallida: %s", exc)
        return pd.DataFrame()


# ── LOAD → SILVER ─────────────────────────────────────────────────────────────

def load_ree_to_silver(df: pd.DataFrame, table_name: str = "clean_prices") -> bool:
    engine = get_engine()
    if engine is None:
        return False
    if df.empty:
        return False

    logger.info("[LOAD] Upsertando %d registro(s) en '%s'", len(df), table_name)
    try:
        df_sql = df.copy()
        df_sql["datetime_utc"]     = pd.to_datetime(df_sql["datetime_utc"], utc=True)
        df_sql["_ingested_at_utc"] = pd.to_datetime(df_sql["_ingested_at_utc"], utc=True)

        with engine.begin() as conn:
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {table_name} (
                    unix_time           BIGINT NOT NULL,
                    datetime_utc        TIMESTAMP WITH TIME ZONE NOT NULL,
                    price_type          TEXT    NOT NULL,
                    price_euro_mwh      DOUBLE PRECISION,
                    _source_file        TEXT,
                    _ingested_at_utc    TIMESTAMP WITH TIME ZONE NOT NULL,
                    PRIMARY KEY (datetime_utc, price_type)
                )
            """))

            cols        = df_sql.columns.tolist()
            pk_cols     = ["datetime_utc", "price_type"]
            update_cols = [c for c in cols if c not in pk_cols]
            update_stmt = ", ".join([f"{c} = EXCLUDED.{c}" for c in update_cols])

            conn.execute(text(f"""
                INSERT INTO {table_name} ({', '.join(cols)})
                VALUES ({', '.join(':' + c for c in cols)})
                ON CONFLICT (datetime_utc, price_type) DO UPDATE SET {update_stmt}
            """), df_sql.to_dict(orient="records"))

        logger.info("[LOAD] '%s' actualizada — %d registro(s)", table_name, len(df))
        return True
    except Exception as exc:
        logger.error("[LOAD] Error escribiendo '%s': %s", table_name, exc)
        return False


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def transform_energy_prices() -> int:
    logger.info("[INIT] ── transform_energy_prices starting ──────────────────")

    all_tasks  = _load_manifest()
    actionable = [t for t in all_tasks if t["status"] in ("pending", "error")]

    if not actionable:
        logger.info("[INIT] Todas las tareas REE ya procesadas")
        return 0

    session_rows = session_ok = session_err = 0

    for task in actionable:
        path_file = task["path"]
        fname     = path_file.split("/")[-1]
        try:
            df_raw = extract_raw_ree_from_json(path_file)
            if df_raw.empty:
                raise ValueError("Bronze file vacío o ilegible")
            df_silver = transform_prices_bronze_to_silver(df_raw)
            if df_silver.empty:
                raise ValueError("Transformación produjo DataFrame vacío")
            rows = len(df_silver)
            if load_ree_to_silver(df_silver):
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
    logger.info("[DONE] transform_energy_prices — ok: %d | errores: %d | filas: %d",
                session_ok, session_err, session_rows)
    return session_rows


if __name__ == "__main__":
    transform_energy_prices()