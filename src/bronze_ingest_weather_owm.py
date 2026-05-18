import os
import json
import requests
import pandas as pd
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import config_paths
from database_utils import get_engine
from logger_config import setup_logging


logger = setup_logging()


# ─────────────────────────────────────────────────────────────────────────────
# WEATHER API KEY desde SSM
# ─────────────────────────────────────────────────────────────────────────────

def _get_weather_api_key() -> Optional[str]:
    """Lee la API key de OpenWeatherMap desde SSM o .env según el entorno."""
    if os.getenv("LOCAL_DEV"):
        from dotenv import load_dotenv
        load_dotenv()
        return os.getenv("WEATHER_API_KEY")
    else:
        import boto3
        from botocore.exceptions import ClientError
        try:
            client   = boto3.client("ssm", region_name=os.getenv("AWS_REGION", "eu-south-2"))
            response = client.get_parameter(Name="/sunsaver/dev/WEATHER_API_KEY", WithDecryption=True)
            return response["Parameter"]["Value"]
        except ClientError as exc:
            logger.error("[EXTRACT] Error leyendo WEATHER_API_KEY de SSM: %s", exc)
            return None


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACT
# ─────────────────────────────────────────────────────────────────────────────

def extract_weather(lat: float, lon: float, api_key: str) -> Dict[str, Any]:
    """Calls the OpenWeatherMap 5-day/3-hour forecast endpoint for one location."""
    params = {
        "lat":   lat,
        "lon":   lon,
        "appid": api_key,
        "units": "metric",
        "lang":  "en",
    }

    try:
        response = requests.get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        if not data:
            raise ValueError("OpenWeatherMap returned an empty payload")

        logger.debug("[EXTRACT] Weather payload received for (lat=%.4f, lon=%.4f)", lat, lon)
        return data

    except Exception as exc:
        logger.error("[EXTRACT] Failed to fetch weather for (lat=%.4f, lon=%.4f): %s", lat, lon, exc)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# INGEST → BRONZE (S3)
# ─────────────────────────────────────────────────────────────────────────────

def ingest_openweather_to_bronze(api_response: dict, client_id: str) -> Optional[str]:
    """
    Persiste el payload de OpenWeatherMap en Bronze.
    En AWS escribe en S3; en LOCAL_DEV escribe en disco.
    Devuelve la clave S3 (o ruta local) del objeto creado.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename  = f"weather_{client_id}_{timestamp}.json"

    if os.getenv("LOCAL_DEV"):
        import stat
        bronze_dir = config_paths.get_bronze_path()
        os.makedirs(bronze_dir, exist_ok=True)
        full_path  = os.path.join(bronze_dir, filename)
        try:
            with open(full_path, "w", encoding="utf-8") as fh:
                json.dump(api_response, fh, ensure_ascii=False, indent=4)
            os.chmod(full_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            logger.info("[BRONZE] Fichero local sellado (chmod 444): %s", filename)
            return full_path
        except Exception as exc:
            logger.error("[BRONZE] Error escribiendo fichero local para client %s: %s", client_id, exc)
            return None
    else:
        s3_key = f"{config_paths.get_bronze_prefix()}weather/{filename}"
        ok = config_paths.write_json_to_s3(api_response, s3_key)
        if ok:
            logger.info("[BRONZE] s3://%s/%s creado", config_paths.S3_BUCKET, s3_key)
            return s3_key
        logger.error("[BRONZE] Error escribiendo en S3 para client %s", client_id)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST
# ─────────────────────────────────────────────────────────────────────────────

def _update_manifest(bronze_dir: str, new_extractions: list) -> None:
    """Actualiza el manifiesto OpenWeather en S3 (o disco en LOCAL_DEV)."""
    if os.getenv("LOCAL_DEV"):
        manifest_path = os.path.join(bronze_dir, "_process_manifest_openweather.json")
        all_tasks: list = []
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as fh:
                    all_tasks = json.load(fh)
            except Exception:
                logger.warning("[MANIFEST] No se pudo parsear el manifiesto OWM — empezando de cero")
        all_tasks.extend(new_extractions)
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(all_tasks, fh, indent=4, ensure_ascii=False)
    else:
        manifest_key = f"{config_paths.get_bronze_prefix()}manifests/_process_manifest_openweather.json"
        try:
            all_tasks = config_paths.read_json_from_s3(manifest_key)
        except Exception:
            all_tasks = []
        all_tasks.extend(new_extractions)
        config_paths.write_json_to_s3(all_tasks, manifest_key)

    pending = sum(1 for t in all_tasks if t["status"] == "pending")
    logger.info(
        "[MANIFEST] OpenWeather manifest updated — new: %d | total pending: %d",
        len(new_extractions), pending,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def extract_openweather(client_table: str = "clean_clients") -> int:
    """
    Punto de entrada: lee clientes desde Silver, fetcha el tiempo para cada
    localización, ingesta en Bronze y actualiza el manifiesto.
    Devuelve el número de ficheros Bronze creados (0 en caso de fallo).
    """
    logger.info("[INIT] ── extract_openweather starting ──────────────────────")

    api_key = _get_weather_api_key()
    if not api_key:
        logger.error("[EXTRACT] WEATHER_API_KEY no disponible — abortando")
        return 0

    engine = get_engine()
    if engine is None:
        return 0

    try:
        query        = f"SELECT client_id, latitude, longitude FROM {client_table}"
        df_clients   = pd.read_sql(query, con=engine)
    except Exception as exc:
        logger.error("[EXTRACT] Error leyendo clientes desde '%s': %s", client_table, exc)
        return 0

    logger.info("[EXTRACT] %d cliente(s) cargados desde '%s'", len(df_clients), client_table)

    new_extractions: list = []
    success_count = error_count = 0

    for _, row in df_clients.iterrows():
        client_id = row["client_id"]
        lat, lon  = row["latitude"], row["longitude"]

        try:
            raw_weather = extract_weather(lat, lon, api_key)
            if not raw_weather:
                logger.warning("[EXTRACT] Payload vacío para client %s — saltando", client_id)
                error_count += 1
                continue

            path_file = ingest_openweather_to_bronze(raw_weather, client_id)
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
            logger.info("[EXTRACT] client_id=%s → Bronze escrito", client_id)

        except Exception as exc:
            logger.error("[EXTRACT] Error procesando client %s: %s", client_id, exc)
            error_count += 1
            continue

    if new_extractions:
        bronze_dir = str(config_paths.get_bronze_path())
        _update_manifest(bronze_dir, new_extractions)

    logger.info(
        "[DONE] extract_openweather finished — creados: %d | errores: %d",
        success_count, error_count,
    )
    return success_count


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    extract_openweather()