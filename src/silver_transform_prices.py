import pandas as pd
import os
from datetime import datetime, timezone
import json
from sqlalchemy import text

import config_paths
from database_utils import get_engine
from logger_config import setup_logging


logger = setup_logging()

MANIFEST_KEY_S3 = "bronze/manifests/_process_manifest_ree.json"


# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _load_manifest() -> list:
    if os.getenv("LOCAL_DEV"):
        bronze_dir    = config_paths.get_bronze_path()
        manifest_path = os.path.join(bronze_dir, "_process_manifest_ree.json")
        if not os.path.exists(manifest_path):
            return []
        with open(manifest_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    else:
        try:
            return config_paths.read_json_from_s3(MANIFEST_KEY_S3)
        except Exception:
            return []


def _save_manifest(tasks: list) -> None:
    if os.getenv("LOCAL_DEV"):
        bronze_dir    = config_paths.get_bronze_path()
        manifest_path = os.path.join(bronze_dir, "_process_manifest_ree.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(tasks, fh, indent=4, ensure_ascii=False)
    else:
        config_paths.write_json_to_s3(tasks, MANIFEST_KEY_S3)


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACT
# ─────────────────────────────────────────────────────────────────────────────

def extract_raw_ree_from_json(file_path: str) -> pd.DataFrame:
    try:
        if os.getenv("LOCAL_DEV"):
            with open(file_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            source_name = os.path.basename(file_path)
        else:
            raw         = config_paths.read_json_from_s3(file_path)
            source_name = file_path.split("/")[-1]

        df = pd.DataFrame([{
            "_ingested_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "_source_file":     source_name,
            "raw_data":         json.dumps(raw),
        }])
        return df

    except Exception as exc:
        logger.error("[EXTRACT] Failed to read Bronze file %s: %s", file_path, exc)
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# TRANSFORM
# ─────────────────────────────────────────────────────────────────────────────

def transform_prices_bronze_to_silver(df_raw: pd.DataFrame) -> pd.DataFrame:
    if df_raw.empty:
        return pd.DataFrame()

    logger.info("[TRANSFORM] Parsing and cleaning REE price data")

    try:
        records = []
        for _, row in df_raw.iterrows():
            raw_json = json.loads(row["raw_data"])
            source   = row["_source_file"]
            ingested = row["_ingested_at_utc"]

            for series in raw_json.get("included", []):
                price_type = series.get("type")
                for v in series.get("attributes", {}).get("values", []):
                    records.append({
                        "price_type":       price_type,
                        "datetime_utc":     v.get("datetime"),
                        "price_euro_mwh":   float(v.get("value")),
                        "_source_file":     source,
                        "_ingested_at_utc": ingested,
                    })

        df = pd.DataFrame(records)
        if df.empty:
            return pd.DataFrame()

        df["datetime_utc"] = pd.to_datetime(df["datetime_utc"], errors="coerce", utc=True)
        df = df.dropna(subset=["datetime_utc"])

        lower, upper = -100, 2000
        outlier_mask = (df["price_euro_mwh"] < lower) | (df["price_euro_mwh"] > upper)
        if outlier_mask.sum():
            logger.warning("[TRANSFORM] %d outlier(s) filtrados", outlier_mask.sum())
            df = df[~outlier_mask]

        df = df.sort_values("_ingested_at_utc", ascending=False)
        df = df.drop_duplicates(subset=["price_type", "datetime_utc"], keep="first")
        df = df.sort_values(["price_type", "datetime_utc"]).reset_index(drop=True)
        df["price_euro_mwh"] = df.groupby("price_type")["price_euro_mwh"].transform(
            lambda x: x.interpolate(method="linear").ffill().bfill().round(4)
        )
        df["unix_time"] = (
            df["datetime_utc"].dt.tz_localize(None)
            .astype("datetime64[s]").astype("int64")
        )

        logger.info("[TRANSFORM] %d Silver price record(s) produced", len(df))
        return df

    except Exception as exc:
        logger.error("[TRANSFORM] Price transformation failed: %s", exc)
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# LOAD → SILVER
# ─────────────────────────────────────────────────────────────────────────────

def load_ree_to_silver(df: pd.DataFrame, table_name: str = "clean_prices") -> bool:
    engine = get_engine()
    if engine is None:
        return False
    if df.empty:
        return False

    logger.info("[LOAD] Upserting %d price record(s) into '%s'", len(df), table_name)

    try:
        df_sql = df.copy()
        df_sql["datetime_utc"]     = pd.to_datetime(df_sql["datetime_utc"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        df_sql["_ingested_at_utc"] = pd.to_datetime(df_sql["_ingested_at_utc"]).dt.strftime("%Y-%m-%d %H:%M:%S")

        with engine.begin() as conn:
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {table_name} (
                    unix_time           INTEGER NOT NULL,
                    datetime_utc        TEXT    NOT NULL,
                    price_type          TEXT    NOT NULL,
                    price_euro_mwh      REAL,
                    _source_file        TEXT,
                    _ingested_at_utc    TEXT    NOT NULL,
                    PRIMARY KEY (datetime_utc, price_type)
                )
            """))
            columns     = df_sql.columns.tolist()
            update_stmt = ", ".join([f"{c} = EXCLUDED.{c}" for c in columns])
            conn.execute(text(f"""
                INSERT INTO {table_name} ({', '.join(columns)})
                VALUES ({', '.join(':' + c for c in columns)})
                ON CONFLICT (datetime_utc, price_type)
                DO UPDATE SET {update_stmt}
            """), df_sql.to_dict(orient="records"))

        logger.info("[LOAD] '%s' updated — %d record(s) upserted", table_name, len(df))
        return True

    except Exception as exc:
        logger.error("[LOAD] Failed to write to '%s': %s", table_name, exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def transform_energy_prices() -> int:
    logger.info("[INIT] ── transform_energy_prices starting ──────────────────")

    all_tasks  = _load_manifest()
    actionable = [t for t in all_tasks if t["status"] in ("pending", "error")]

    if not actionable:
        logger.info("[INIT] All REE manifest tasks already processed")
        return 0

    session_rows = session_ok = session_err = 0

    for task in actionable:
        path_file = task["path"]
        fname     = path_file.split("/")[-1]

        try:
            df_raw = extract_raw_ree_from_json(path_file)
            if df_raw.empty:
                raise ValueError("Bronze file empty or unreadable")

            df_silver = transform_prices_bronze_to_silver(df_raw)
            if df_silver.empty:
                raise ValueError("Transformation produced empty DataFrame")

            rows = len(df_silver)
            if load_ree_to_silver(df_silver):
                task.update({"status": "success", "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
                task.pop("error", None)
                session_rows += rows
                session_ok   += 1
            else:
                raise ValueError("Silver load returned False")

        except Exception as exc:
            task.update({"status": "error", "error": str(exc),
                         "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
            session_err += 1
            logger.error("[ERROR] %s failed: %s", fname, exc)

    _save_manifest(all_tasks)

    logger.info("[DONE] transform_energy_prices — ok: %d | errors: %d | rows: %d",
                session_ok, session_err, session_rows)
    return session_rows


if __name__ == "__main__":
    transform_energy_prices()