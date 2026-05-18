import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd
import requests
from dotenv import load_dotenv

import config_paths
from database_utils import get_engine
from logger_config import setup_logging

load_dotenv()
logger = setup_logging()


# ── API KEY ───────────────────────────────────────────────────────────────────

def _get_weather_api_key() -> Optional[str]:
    """Lee WEATHER_API_KEY del entorno (viene del .env en local o de SSM vía ECS en Fargate)."""
    key = os.getenv("WEATHER_API_KEY")
    if not key:
        logger.error("[EXTRACT] WEATHER_API_KEY no encontrada en el entorno")
    return key


# ── EXTRACT ───────────────────────────────────────────────────────────────────

def extract_weather(lat: float, lon: float, api_key: str) -> Dict[str, Any]:
    params = {"lat": lat, "lon": lon, "appid": api_key, "units": "metric", "lang": "en"}
    try:
        response = requests.get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params=params, timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if not data:
            raise ValueError("OpenWeatherMap devolvió payload vacío")
        logger.debug("[EXTRACT] Weather recibido para (lat=%.4f, lon=%.4f)", lat, lon)
        return data
    except Exception as exc:
        logger.error("[EXTRACT] Error weather (lat=%.4f, lon=%.4f): %s", lat, lon, exc)
        raise


# ── INGEST → BRONZE (S3) ──────────────────────────────────────────────────────

def ingest_openweather_to_bronze(api_response: dict, client_id: str) -> Optional[str]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    s3_key    = f"{config_paths.get_bronze_prefix()}weather/weather_{client_id}_{timestamp}.json"

    ok = config_paths.write_json_to_s3(api_response, s3_key)
    if ok:
        logger.info("[BRONZE] s3://%s/%s creado", config_paths.S3_BUCKET, s3_key)
        return s3_key

    logger.error("[BRONZE] Error escribiendo en S3 para client %s", client_id)
    return None


# ── MANIFEST ──────────────────────────────────────────────────────────────────

def _update_manifest(new_extractions: list) -> None:
    manifest_key = f"{config_paths.get_bronze_prefix()}manifests/_process_manifest_openweather.json"
    try:
        all_tasks = config_paths.read_json_from_s3(manifest_key)
    except Exception:
        all_tasks = []
    all_tasks.extend(new_extractions)
    config_paths.write_json_to_s3(all_tasks, manifest_key)
    pending = sum(1 for t in all_tasks if t["status"] == "pending")
    logger.info("[MANIFEST] OpenWeather actualizado — nuevos: %d | pendientes: %d",
                len(new_extractions), pending)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def extract_openweather(client_table: str = "clean_clients") -> int:
    logger.info("[INIT] ── extract_openweather starting ──────────────────────")

    api_key = _get_weather_api_key()
    if not api_key:
        return 0

    engine = get_engine()
    if engine is None:
        return 0

    try:
        import pandas as pd
        df_clients = pd.read_sql(
            f"SELECT client_id, latitude, longitude FROM {client_table}", con=engine
        )
    except Exception as exc:
        logger.error("[EXTRACT] Error leyendo clientes desde '%s': %s", client_table, exc)
        return 0

    logger.info("[EXTRACT] %d cliente(s) cargados", len(df_clients))

    new_extractions: list = []
    success_count = error_count = 0

    for _, row in df_clients.iterrows():
        client_id = row["client_id"]
        try:
            raw_weather = extract_weather(row["latitude"], row["longitude"], api_key)
            path_file   = ingest_openweather_to_bronze(raw_weather, client_id)
            if not path_file:
                error_count += 1
                continue
            new_extractions.append({
                "source":     "openweather",
                "client_id":  client_id,
                "path":       path_file,
                "status":     "pending",
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            })
            success_count += 1
            logger.info("[EXTRACT] client_id=%s → Bronze OK", client_id)
        except Exception as exc:
            logger.error("[EXTRACT] Error client %s: %s", client_id, exc)
            error_count += 1

    if new_extractions:
        _update_manifest(new_extractions)

    logger.info("[DONE] extract_openweather — creados: %d | errores: %d",
                success_count, error_count)
    return success_count


if __name__ == "__main__":
    extract_openweather()