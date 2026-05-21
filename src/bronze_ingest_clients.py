import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

import config_paths
from logger_config import setup_logging

load_dotenv()
logger = setup_logging()

CLIENTS_S3_KEY = "inputs/clients_source.xlsx"


# ── EXTRACT ───────────────────────────────────────────────────────────────────

def extract_clients_from_excel() -> list[dict]:
    """Descarga el Excel de clientes desde S3 y devuelve los registros raw."""
    excel_path = config_paths.get_client_path()

    logger.info("[EXTRACT] Descargando Excel desde S3: s3://%s/%s",
                config_paths.S3_BUCKET, CLIENTS_S3_KEY)

    ok = config_paths.download_from_s3(CLIENTS_S3_KEY, str(excel_path))
    if not ok:
        logger.error("[EXTRACT] No se pudo descargar el Excel desde S3 "
                     "(s3://%s/%s)", config_paths.S3_BUCKET, CLIENTS_S3_KEY)
        return []

    try:
        df = pd.read_excel(excel_path, sheet_name="Clients Data")
    except ImportError:
        logger.error("[EXTRACT] Falta dependencia 'openpyxl' — pip install openpyxl")
        return []
    except Exception as exc:
        logger.error("[EXTRACT] Error leyendo Excel: %s", exc)
        return []

    if df.empty:
        logger.warning("[EXTRACT] El Excel está vacío")
        return []

    df = df.astype(object).where(pd.notnull(df), None)
    logger.info("[EXTRACT] %d registro(s) de cliente leídos", len(df))
    return df.to_dict(orient="records")


# ── INGEST → BRONZE (S3) ──────────────────────────────────────────────────────

def ingest_clients_to_bronze(records: list[dict]) -> Optional[str]:
    """Persiste los registros raw en Bronze (S3). Devuelve la clave S3 creada."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename  = f"clients_{timestamp}.json"
    s3_key    = f"{config_paths.get_bronze_prefix()}clients/{filename}"

    logger.info("[BRONZE] Escribiendo %d registro(s) → s3://%s/%s",
                len(records), config_paths.S3_BUCKET, s3_key)

    ok = config_paths.write_json_to_s3(records, s3_key)
    if ok:
        logger.info("[BRONZE] Objeto S3 creado: %s", s3_key)
        return s3_key

    logger.error("[BRONZE] Error escribiendo en S3: %s", s3_key)
    return None


# ── MANIFEST ──────────────────────────────────────────────────────────────────

def _update_manifest(path_file: str) -> None:
    manifest_key = f"{config_paths.get_bronze_prefix()}manifests/_process_manifest_clients.json"
    new_task = {
        "source":     "clients_source.xlsx",
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
    logger.info("[MANIFEST] Clientes actualizado — pendientes: %d", pending)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def extract_clients() -> int:
    logger.info("[INIT] ── extract_clients starting ──────────────────────────")

    raw_clients = extract_clients_from_excel()
    if not raw_clients:
        logger.warning("[INIT] Sin registros extraídos — abortando")
        return 0

    path_file = ingest_clients_to_bronze(raw_clients)
    if not path_file:
        logger.error("[INIT] Fallo en ingesta Bronze — abortando")
        return 0

    _update_manifest(path_file)

    total = len(raw_clients)
    logger.info("[DONE] extract_clients finished — registros: %d", total)
    return total


if __name__ == "__main__":
    extract_clients()