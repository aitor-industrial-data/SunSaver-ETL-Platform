import requests
import os
from datetime import datetime, timedelta, timezone
import json
from typing import Optional, Union

import config_paths
from logger_config import setup_logging


logger = setup_logging()


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACT
# ─────────────────────────────────────────────────────────────────────────────

def extract_raw_json_from_ree() -> Union[dict, bool]:
    """
    Fetches tomorrow's PVPC prices (id=1001) from Red Eléctrica de España.
    Returns the raw API payload or False when data is not yet published.
    """
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    url = (
        "https://apidatos.ree.es/es/datos/mercados/precios-mercados-tiempo-real"
        f"?start_date={tomorrow}T00:00&end_date={tomorrow}T23:59"
        "&time_trunc=hour&geo_trunc=electric_system"
        "&geo_limit=peninsular&geo_ids=8741"
    )
    headers = {
        "Accept":   "application/json",
        "Origin":   "https://www.ree.es",
        "Referer":  "https://www.ree.es/",
    }

    logger.info("[EXTRACT] Requesting PVPC prices for %s from REE API", tomorrow)

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        all_data = response.json()

        pvpc_item = next(
            (item for item in all_data.get("included", []) if item.get("id") == "1001"),
            None,
        )

        if pvpc_item and pvpc_item["attributes"].get("values"):
            n_values = len(pvpc_item["attributes"]["values"])
            logger.info("[EXTRACT] PVPC data retrieved — %d hourly values for %s", n_values, tomorrow)
            all_data["included"] = [pvpc_item]
            return all_data

        logger.warning(
            "[EXTRACT] REE returned no PVPC values for %s "
            "(prices are typically published after 20:30 CET)", tomorrow,
        )
        return False

    except requests.exceptions.HTTPError as exc:
        code = response.status_code
        if code in (500, 502, 503, 504):
            logger.error("[EXTRACT] REE server unavailable (HTTP %d)", code)
        else:
            logger.error("[EXTRACT] Unexpected HTTP error from REE (HTTP %d): %s", code, exc)
        return False

    except Exception as exc:
        logger.error("[EXTRACT] Unexpected error contacting REE API: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# INGEST → BRONZE (S3)
# ─────────────────────────────────────────────────────────────────────────────

def ingest_ree_to_bronze(api_response: dict) -> Optional[str]:
    """
    Persiste el payload REE en la capa Bronze.
    En AWS escribe directamente en S3; en LOCAL_DEV escribe en disco.
    Devuelve la clave S3 (o ruta local) del objeto creado.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename  = f"prices_{timestamp}.json"

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
            logger.error("[BRONZE] Error escribiendo fichero local: %s", exc)
            return None
    else:
        s3_key = f"{config_paths.get_bronze_prefix()}prices/{filename}"
        logger.info("[BRONZE] Escribiendo REE payload → s3://%s/%s",
                    config_paths.S3_BUCKET, s3_key)
        ok = config_paths.write_json_to_s3(api_response, s3_key)
        if ok:
            logger.info("[BRONZE] Objeto S3 creado: %s", s3_key)
            return s3_key
        logger.error("[BRONZE] Error escribiendo en S3: %s", s3_key)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST
# ─────────────────────────────────────────────────────────────────────────────

def _update_manifest(bronze_dir: str, path_file: str) -> None:
    """Actualiza el manifiesto REE en S3 (o disco en LOCAL_DEV)."""
    new_task = {
        "source":     "REE",
        "path":       path_file,
        "status":     "pending",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }

    if os.getenv("LOCAL_DEV"):
        manifest_path = os.path.join(bronze_dir, "_process_manifest_ree.json")
        all_tasks: list = []
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as fh:
                    all_tasks = json.load(fh)
            except Exception:
                logger.warning("[MANIFEST] No se pudo parsear el manifiesto REE — empezando de cero")
        all_tasks.append(new_task)
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(all_tasks, fh, indent=4, ensure_ascii=False)
    else:
        manifest_key = f"{config_paths.get_bronze_prefix()}manifests/_process_manifest_ree.json"
        try:
            all_tasks = config_paths.read_json_from_s3(manifest_key)
        except Exception:
            all_tasks = []
        all_tasks.append(new_task)
        config_paths.write_json_to_s3(all_tasks, manifest_key)

    pending = sum(1 for t in all_tasks if t["status"] == "pending")
    logger.info("[MANIFEST] REE manifest updated — pending tasks: %d", pending)


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def extract_energy_prices() -> Union[int, bool]:
    """
    Punto de entrada: fetch PVPC → ingesta Bronze → actualiza manifiesto.
    Devuelve int (nº de registros) o False si REE no tiene datos disponibles.
    """
    logger.info("[INIT] ── extract_energy_prices starting ────────────────────")

    raw_prices = extract_raw_json_from_ree()
    if raw_prices is False:
        logger.warning("[INIT] No REE data available — signalling PARTIAL SUCCESS")
        return False

    try:
        total_hours = len(raw_prices["included"][0]["attributes"]["values"])
    except (KeyError, IndexError):
        logger.error("[EXTRACT] Unrecognised REE payload structure")
        return False

    path_file = ingest_ree_to_bronze(raw_prices)
    if not path_file:
        logger.error("[BRONZE] Ingestion failed — aborting")
        return False

    bronze_dir = str(config_paths.get_bronze_path())
    _update_manifest(bronze_dir, path_file)

    logger.info("[DONE] extract_energy_prices finished — hourly records: %d", total_hours)
    return total_hours


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    extract_energy_prices()