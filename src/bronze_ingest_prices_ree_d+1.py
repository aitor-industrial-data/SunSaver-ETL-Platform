import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import requests
from dotenv import load_dotenv

import config_paths
from logger_config import setup_logging

load_dotenv()
logger = setup_logging()


# ── EXTRACT ───────────────────────────────────────────────────────────────────

def extract_raw_json_from_ree() -> Union[dict, bool]:
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    url = (
        "https://apidatos.ree.es/es/datos/mercados/precios-mercados-tiempo-real"
        f"?start_date={tomorrow}T00:00&end_date={tomorrow}T23:59"
        "&time_trunc=hour&geo_trunc=electric_system"
        "&geo_limit=peninsular&geo_ids=8741"
    )
    headers = {
        "Accept":  "application/json",
        "Origin":  "https://www.ree.es",
        "Referer": "https://www.ree.es/",
    }
    logger.info("[EXTRACT] Requesting PVPC prices for %s from REE API", tomorrow)

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        all_data  = response.json()
        pvpc_item = next(
            (i for i in all_data.get("included", []) if i.get("id") == "1001"), None
        )
        if pvpc_item and pvpc_item["attributes"].get("values"):
            n = len(pvpc_item["attributes"]["values"])
            logger.info("[EXTRACT] PVPC data retrieved — %d hourly values for %s", n, tomorrow)
            all_data["included"] = [pvpc_item]
            return all_data

        logger.warning("[EXTRACT] REE sin valores PVPC para %s (publicación tras 20:30 CET)", tomorrow)
        return False

    except requests.exceptions.HTTPError as exc:
        logger.error("[EXTRACT] HTTP error REE (%d): %s", response.status_code, exc)
        return False
    except Exception as exc:
        logger.error("[EXTRACT] Error contactando REE API: %s", exc)
        return False


# ── INGEST → BRONZE (S3) ──────────────────────────────────────────────────────

def ingest_ree_to_bronze(api_response: dict) -> Optional[str]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    s3_key    = f"{config_paths.get_bronze_prefix()}prices/prices_{timestamp}.json"

    logger.info("[BRONZE] Escribiendo REE payload → s3://%s/%s",
                config_paths.S3_BUCKET, s3_key)
    ok = config_paths.write_json_to_s3(api_response, s3_key)
    if ok:
        logger.info("[BRONZE] Objeto S3 creado: %s", s3_key)
        return s3_key

    logger.error("[BRONZE] Error escribiendo en S3: %s", s3_key)
    return None


# ── MANIFEST ──────────────────────────────────────────────────────────────────

def _update_manifest(path_file: str) -> None:
    manifest_key = f"{config_paths.get_bronze_prefix()}manifests/_process_manifest_ree.json"
    new_task = {
        "source":     "REE",
        "path":       path_file,
        "status":     "pending",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        all_tasks = config_paths.read_json_from_s3(manifest_key)
    except Exception:
        all_tasks = []
    all_tasks.append(new_task)
    config_paths.write_json_to_s3(all_tasks, manifest_key)
    pending = sum(1 for t in all_tasks if t["status"] == "pending")
    logger.info("[MANIFEST] REE actualizado — pendientes: %d", pending)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def extract_energy_prices() -> Union[int, bool]:
    logger.info("[INIT] ── extract_energy_prices starting ────────────────────")

    raw_prices = extract_raw_json_from_ree()
    if raw_prices is False:
        logger.warning("[INIT] Sin datos REE — PARTIAL SUCCESS")
        return False

    try:
        total_hours = len(raw_prices["included"][0]["attributes"]["values"])
    except (KeyError, IndexError):
        logger.error("[EXTRACT] Estructura REE desconocida")
        return False

    path_file = ingest_ree_to_bronze(raw_prices)
    if not path_file:
        logger.error("[BRONZE] Ingesta fallida — abortando")
        return False

    _update_manifest(path_file)
    logger.info("[DONE] extract_energy_prices — registros horarios: %d", total_hours)
    return total_hours


if __name__ == "__main__":
    extract_energy_prices()