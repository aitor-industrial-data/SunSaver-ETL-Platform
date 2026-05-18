import requests
import stat
import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import json
from typing import Optional, Union

import config_paths
from logger_config import setup_logging


logger = setup_logging()
load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────

API_KEY = os.getenv("ESIOS_API_KEY")
BASE_URL = "https://api.esios.ree.es/indicators"
GEO_ID_PENINSULAR = 8741

# Pedimos datos de AYER (D-1) para garantizar que todos están consolidados.
# Lanzar junto con bronze_ingest_prices_d1.py a las 20:30 CET.
INDICATORS = {
    "upward_imb":   685,    # Desvío a Subir (€/MWh)
    "downward_imb": 686,    # Desvío a Bajar (€/MWh)
    "imb_price":    687,    # Precio Medio de los Desvíos (€/MWh)
    "demand_real":  1293,    # Demanda Real de Energía (MWh)
    "pv_gen":       1295,  # Generación Fotovoltaica Nacional (MWh)
    "wind_gen":     10034,  # Generación Eólica Total (MWh)
    "co2":          10299,  # CO2 Asociado a la Generación (tCO2eq/MWh)
    "exchanges":    10211,  # Intercambios Internacionales Netos (MWh)
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


def _fetch_indicator(indicator_id: int, name: str, target_date: datetime, headers: dict) -> Optional[dict]:
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

    yesterday = datetime.now() - timedelta(days=1)
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
# INGEST → BRONZE
# ─────────────────────────────────────────────────────────────────────────────

def ingest_to_bronze(payload: dict) -> Optional[str]:
    bronze_dir = config_paths.get_bronze_path()
    os.makedirs(bronze_dir, exist_ok=True)

    ts        = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename  = f"context_d1_{ts}.json"
    full_path = os.path.join(bronze_dir, filename)

    try:
        with open(full_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=4)
        os.chmod(full_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        logger.info("[BRONZE] Sealed (chmod 444): %s", filename)
        return full_path
    except Exception as exc:
        logger.error("[BRONZE] Persistence failed: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST
# ─────────────────────────────────────────────────────────────────────────────

def _update_manifest(bronze_dir: str, path_file: str) -> None:
    manifest_path = os.path.join(bronze_dir, "_process_manifest_esios_d1_context.json")

    new_task = {
        "source":     "ESIOS_D1_CONTEXT",
        "path":       path_file,
        "status":     "pending",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }

    all_tasks: list = []
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as fh:
                all_tasks = json.load(fh)
        except Exception:
            logger.warning("[MANIFEST] No se pudo leer — empezando de cero")

    all_tasks.append(new_task)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(all_tasks, fh, indent=4, ensure_ascii=False)

    pending = sum(1 for t in all_tasks if t["status"] == "pending")
    logger.info("[MANIFEST] esios_d1_context updated — pending: %d", pending)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def extract_system_context() -> Union[int, bool]:
    """
    Extrae contexto del sistema de AYER (D-1) → Bronze → manifest.
    Lanzar a las 20:30 CET junto con bronze_ingest_prices_d1.py.
    Al pedir D-1 todos los indicadores están siempre consolidados, incluido pv_gen.

    Returns:
        int   — número de indicadores ingestados.
        False — ningún indicador disponible (→ PARTIAL SUCCESS).
    """
    logger.info("[INIT] ── extract_system_context D-1 starting ──")

    payload = extract_yesterday_context()
    if payload is None:
        logger.warning("[INIT] Sin datos D-1 — PARTIAL SUCCESS")
        return False

    path_file = ingest_to_bronze(payload)
    if not path_file:
        logger.error("[BRONZE] Ingestion failed — aborting")
        return False

    _update_manifest(str(config_paths.get_bronze_path()), path_file)

    n_ok = sum(1 for v in payload["indicators"].values() if v is not None)
    logger.info("[DONE] D-1 context indicators ingested: %d/%d", n_ok, len(payload["indicators"]))
    return n_ok


if __name__ == "__main__":
    extract_system_context()