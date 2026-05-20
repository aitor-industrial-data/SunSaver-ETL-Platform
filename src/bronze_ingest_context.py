import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import requests
from dotenv import load_dotenv
import os

import config_paths
from logger_config import setup_logging

load_dotenv()
logger = setup_logging()


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────

API_KEY  = os.getenv("ESIOS_API_KEY")
BASE_URL = "https://api.esios.ree.es/indicators"
GEO_ID_PENINSULAR = 8741

# Pedimos D-1 para garantizar que todos los indicadores están consolidados.
# Lanzar a las 20:30 CET junto con bronze_ingest_prices_d1.py.
#
# IDs verificados contra la API (19/05/2026):
#   1293  — Demanda Real peninsular (MWh)              ✓ 24 valores
#   1295  — Generación Fotovoltaica T.Real (MWh)       ✓ 24 valores
#   10355 — CO2 Asociado Generación T.Real (tCO2/MWh)  ✓ 24 valores
#   685   — Desvío a Subir (€/MWh)                     ✓ 24 valores
#

INDICATORS = {
    "demand_real": 1293,
    "pv_gen":      1295,
    "co2":         10355,
    "upward_imb":  685,
}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_headers() -> dict:
    if not API_KEY:
        raise EnvironmentError("ESIOS_API_KEY no definida en .env")
    return {
        "Accept":       "application/json; application/vnd.esios-api-v1+json",
        "Content-Type": "application/json",
        "x-api-key":    API_KEY,
    }


def _fetch_indicator(
    indicator_id: int,
    name: str,
    target_date: datetime,
    headers: dict,
) -> Optional[dict]:
    """Solicita un indicador horario para target_date en UTC."""
    start = target_date.strftime("%Y-%m-%dT00:00")
    end   = target_date.strftime("%Y-%m-%dT23:59")
    url   = (
        f"{BASE_URL}/{indicator_id}"
        f"?start_date={start}&end_date={end}"
        f"&time_trunc=hour&geo_ids[]={GEO_ID_PENINSULAR}"
    )
    label = f"[EXTRACT] {indicator_id} ({name}) {target_date.date()}"
    logger.info("%s — requesting", label)

    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code in (403, 404):
            logger.error("%s — HTTP %d", label, r.status_code)
            return None
        r.raise_for_status()
        values = r.json().get("indicator", {}).get("values", [])
        if not values:
            logger.warning("%s — sin valores", label)
            return None
        logger.info("%s — %d valores horarios recibidos", label, len(values))
        return r.json()
    except Exception as exc:
        logger.error("%s — error: %s", label, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACT
# ─────────────────────────────────────────────────────────────────────────────

def extract_yesterday_context() -> Optional[dict]:
    try:
        headers = _build_headers()
    except EnvironmentError as exc:
        logger.error("[EXTRACT] %s", exc)
        return None

    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    logger.info("[EXTRACT] ── D-1 context extraction  target=%s ──", yesterday.date())

    results: dict[str, Optional[dict]] = {}
    for name, ind_id in INDICATORS.items():
        results[name] = _fetch_indicator(ind_id, name, yesterday, headers)

    ok  = [k for k, v in results.items() if v is not None]
    nok = [k for k, v in results.items() if v is None]
    logger.info("[EXTRACT] OK: %s", ok)
    if nok:
        logger.warning("[EXTRACT] Sin datos (null): %s", nok)

    if not ok:
        logger.error("[EXTRACT] Ningún indicador disponible — abortando")
        return None

    return {
        "fetch_ts":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target_date": yesterday.strftime("%Y-%m-%d"),
        "scope":       "D-1",
        "indicators":  results,
    }


# ─────────────────────────────────────────────────────────────────────────────
# INGEST → BRONZE (S3)
# ─────────────────────────────────────────────────────────────────────────────

def ingest_to_bronze(payload: dict) -> Optional[str]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    s3_key    = f"{config_paths.get_bronze_prefix()}context/context_{timestamp}.json"

    logger.info("[BRONZE] Escribiendo payload → s3://%s/%s",
                config_paths.S3_BUCKET, s3_key)
    ok = config_paths.write_json_to_s3(payload, s3_key)
    if ok:
        logger.info("[BRONZE] Objeto S3 creado: %s", s3_key)
        return s3_key

    logger.error("[BRONZE] Error escribiendo en S3: %s", s3_key)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST
# ─────────────────────────────────────────────────────────────────────────────

def _update_manifest(path_file: str) -> None:
    manifest_key = f"{config_paths.get_bronze_prefix()}manifests/_process_manifest_esios_context.json"

    new_task = {
        "source":     "ESIOS_CONTEXT",
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
    logger.info("[MANIFEST] esios_context actualizado — pendientes: %d", pending)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def extract_system_context() -> Union[int, bool]:
    """
    Extrae contexto del sistema eléctrico de AYER (D-1) → S3 Bronze → manifest.
    Lanzar a las 20:30 CET junto con bronze_ingest_prices_d1.py.

    Indicadores: demanda real, fotovoltaica, eólica, CO2, desvío a subir.
    Todos consolidados en D-1 — no hay riesgo de valores parciales.

    Returns:
        int   — número de indicadores ingestados con éxito.
        False — ningún indicador disponible (→ PARTIAL SUCCESS).
    """
    logger.info("[INIT] ── extract_system_context D-1 starting ──────────────")

    payload = extract_yesterday_context()
    if payload is None:
        logger.warning("[INIT] Sin datos D-1 — PARTIAL SUCCESS")
        return False

    path_file = ingest_to_bronze(payload)
    if not path_file:
        logger.error("[BRONZE] Ingesta fallida — abortando")
        return False

    _update_manifest(path_file)

    n_ok = sum(1 for v in payload["indicators"].values() if v is not None)
    logger.info(
        "[DONE] D-1 context indicators ingested: %d/%d",
        n_ok, len(payload["indicators"]),
    )
    return n_ok


if __name__ == "__main__":
    extract_system_context()