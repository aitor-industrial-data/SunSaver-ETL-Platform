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

MANIFEST_KEY_S3 = "bronze/manifests/_process_manifest_openweather.json"


# ── MANIFEST ──────────────────────────────────────────────────────────────────

def _load_manifest() -> list:
    try:
        return config_paths.read_json_from_s3(MANIFEST_KEY_S3)
    except Exception:
        return []


def _save_manifest(tasks: list) -> None:
    config_paths.write_json_to_s3(tasks, MANIFEST_KEY_S3)


# ── EXTRACT ───────────────────────────────────────────────────────────────────

def extract_raw_weather_from_json(file_path: str, client_id: str) -> pd.DataFrame:
    try:
        raw         = config_paths.read_json_from_s3(file_path)
        source_name = file_path.split("/")[-1]
        return pd.DataFrame([{
            "client_id":        client_id,
            "_ingested_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "_source_file":     source_name,
            "raw_data":         json.dumps(raw),
        }])
    except Exception as exc:
        logger.error("[EXTRACT] Error leyendo %s: %s", file_path, exc)
        return pd.DataFrame()


# ── TRANSFORM ─────────────────────────────────────────────────────────────────

def transform_weather_bronze_to_silver(df_raw: pd.DataFrame) -> pd.DataFrame:
    if df_raw.empty:
        return pd.DataFrame()

    all_clients = []
    try:
        for _, row in df_raw.iterrows():
            client_id = row["client_id"]
            raw_json  = json.loads(row["raw_data"])
            forecasts = raw_json.get("list", [])

            records = [{
                "forecast_time_utc":   f.get("dt_txt"),
                "temp_celsius":        f.get("main", {}).get("temp"),
                "humidity_pct":        f.get("main", {}).get("humidity"),
                "clouds_pct":          f.get("clouds", {}).get("all"),
                "rain_prob_norm":      f.get("pop"),
                "wind_speed_mps":      f.get("wind", {}).get("speed"),
                "weather_id":          f.get("weather", [{}])[0].get("id"),
                "weather_main":        f.get("weather", [{}])[0].get("main"),
                "weather_description": f.get("weather", [{}])[0].get("description"),
                "pod":                 f.get("sys", {}).get("pod"),
            } for f in forecasts]

            df_c = pd.DataFrame(records)
            df_c["forecast_time_utc"] = pd.to_datetime(df_c["forecast_time_utc"])
            df_c = df_c.drop_duplicates(subset=["forecast_time_utc"], keep="last")
            df_c = df_c.set_index("forecast_time_utc").resample("1h").asfreq()

            num_cols = ["temp_celsius", "humidity_pct", "clouds_pct", "rain_prob_norm", "wind_speed_mps"]
            df_c[num_cols] = df_c[num_cols].interpolate(method="linear").round(3)
            df_c[["weather_id", "weather_main", "weather_description", "pod"]] = \
                df_c[["weather_id", "weather_main", "weather_description", "pod"]].ffill()

            df_c = df_c.reset_index()
            df_c["client_id"]        = client_id
            df_c["_ingested_at_utc"] = row["_ingested_at_utc"]
            df_c["_source_file"]     = row["_source_file"]
            df_c["unix_time"]        = (
                (df_c["forecast_time_utc"] - pd.Timestamp("1970-01-01")) // pd.Timedelta("1s")
            )
            df_c["is_daylight"] = df_c["pod"].apply(lambda x: 1 if x == "d" else 0)
            all_clients.append(df_c)

        df_final = pd.concat(all_clients, ignore_index=True)
        df_final["rain_prob_norm"]   = df_final["rain_prob_norm"].fillna(0)
        df_final["_ingested_at_utc"] = pd.to_datetime(df_final["_ingested_at_utc"], errors="coerce")
        df_final = df_final.dropna(subset=["client_id", "forecast_time_utc"])
        if "pod" in df_final.columns:
            df_final = df_final.drop(columns=["pod"])

        logger.info("[TRANSFORM] %d filas Silver weather producidas", len(df_final))
        return df_final

    except Exception as exc:
        logger.error("[TRANSFORM] Transformación weather fallida: %s", exc)
        return pd.DataFrame()


# ── LOAD → SILVER ─────────────────────────────────────────────────────────────

def load_weather_to_silver(df: pd.DataFrame, table_name: str = "clean_weather") -> bool:
    engine = get_engine()
    if engine is None:
        return False
    if df.empty:
        return False

    logger.info("[LOAD] Upsertando %d registro(s) en '%s'", len(df), table_name)
    try:
        df_sql = df.copy()
        df_sql["forecast_time_utc"] = df_sql["forecast_time_utc"].dt.strftime("%Y-%m-%d %H:%M:%S")
        df_sql["_ingested_at_utc"]  = df_sql["_ingested_at_utc"].dt.strftime("%Y-%m-%d %H:%M:%S")

        with engine.begin() as conn:
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {table_name} (
                    client_id               TEXT    NOT NULL,
                    unix_time               INTEGER NOT NULL,
                    forecast_time_utc       TEXT    NOT NULL,
                    temp_celsius            REAL,
                    humidity_pct            REAL,
                    clouds_pct              REAL,
                    rain_prob_norm          REAL,
                    wind_speed_mps          REAL,
                    weather_id              INTEGER,
                    weather_main            TEXT,
                    weather_description     TEXT,
                    is_daylight             INTEGER,
                    _source_file            TEXT,
                    _ingested_at_utc        TEXT NOT NULL,
                    PRIMARY KEY (client_id, unix_time)
                )
            """))
            cols        = list(df_sql.columns)
            update_cols = [c for c in cols if c not in ["client_id", "unix_time"]]
            update_stmt = ", ".join([f"{c} = EXCLUDED.{c}" for c in update_cols])
            conn.execute(text(f"""
                INSERT INTO {table_name} ({', '.join(cols)})
                VALUES ({', '.join(':' + c for c in cols)})
                ON CONFLICT (client_id, unix_time) DO UPDATE SET {update_stmt}
            """), df_sql.to_dict(orient="records"))

        logger.info("[LOAD] '%s' actualizada — %d registro(s)", table_name, len(df))
        return True
    except Exception as exc:
        logger.error("[LOAD] Error escribiendo '%s': %s", table_name, exc)
        return False


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def transform_openweather() -> int:
    logger.info("[INIT] ── transform_openweather starting ────────────────────")

    all_tasks  = _load_manifest()
    actionable = [t for t in all_tasks if t["status"] in ("pending", "error")]

    if not actionable:
        logger.info("[INIT] Todas las tareas OpenWeather ya procesadas")
        return 0

    session_rows = session_ok = session_err = 0

    for task in actionable:
        client_id = task["client_id"]
        path_file = task["path"]
        fname     = path_file.split("/")[-1]
        try:
            df_raw = extract_raw_weather_from_json(path_file, client_id)
            if df_raw.empty:
                raise ValueError("Bronze file vacío o ilegible")
            df_silver = transform_weather_bronze_to_silver(df_raw)
            if df_silver.empty:
                raise ValueError("Transformación produjo DataFrame vacío")
            rows = len(df_silver)
            if load_weather_to_silver(df_silver):
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
            logger.error("[ERROR] client_id=%s %s: %s", client_id, fname, exc)

    _save_manifest(all_tasks)
    logger.info("[DONE] transform_openweather — ok: %d | errores: %d | filas: %d",
                session_ok, session_err, session_rows)
    return session_rows


if __name__ == "__main__":
    transform_openweather()