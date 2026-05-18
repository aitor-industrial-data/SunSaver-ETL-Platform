import os
import json
import stat
import pandas as pd
from typing import Optional
from pathlib import Path
from datetime import datetime, timezone

import config_paths
from logger_config import setup_logging


logger = setup_logging()

# Clave S3 donde debe estar el Excel de clientes
CLIENTS_S3_KEY = "inputs/clients_source.xlsx"


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACT
# ─────────────────────────────────────────────────────────────────────────────

def extract_clients_from_excel() -> list[dict]:
    """
    Descarga el Excel de clientes desde S3 a /tmp y devuelve los registros raw.
    En local (LOCAL_DEV=1) lo lee desde disco como antes.
    """
    if os.getenv("LOCAL_DEV"):
        BASE_DIR   = Path(__file__).resolve().parent.parent
        excel_path = BASE_DIR / "data" / "clients_source.xlsx"
        logger.info("[EXTRACT] Modo LOCAL_DEV — leyendo desde disco: %s", excel_path)
    else:
        excel_path = config_paths.get_client_path()   # /tmp/clients_source.xlsx
        logger.info("[EXTRACT] Descargando Excel desde S3: s3://%s/%s",
                    config_paths.S3_BUCKET, CLIENTS_S3_KEY)

        ok = config_paths.download_from_s3(CLIENTS_S3_KEY, str(excel_path))
        if not ok:
            logger.error("[EXTRACT] No se pudo descargar el Excel desde S3. "
                         "Asegúrate de haber subido el fichero a "
                         "s3://%s/%s", config_paths.S3_BUCKET, CLIENTS_S3_KEY)
            return []

    if not excel_path.exists():
        logger.error("[EXTRACT] Fichero no encontrado: %s", excel_path)
        return []

    try:
        df = pd.read_excel(excel_path)
    except ImportError:
        logger.error("[EXTRACT] Falta la dependencia 'openpyxl' — ejecuta: pip install openpyxl")
        return []
    except Exception as exc:
        logger.error("[EXTRACT] Error leyendo el Excel: %s", exc)
        return []

    if df.empty:
        logger.warning("[EXTRACT] El Excel está vacío: %s", excel_path.name)
        return []

    df = df.astype(object).where(pd.notnull(df), None)
    logger.info("[EXTRACT] %d registro(s) de cliente leídos", len(df))
    return df.to_dict(orient="records")


# ─────────────────────────────────────────────────────────────────────────────
# INGEST → BRONZE (S3)
# ─────────────────────────────────────────────────────────────────────────────

def ingest_clients_to_bronze(records: list[dict]) -> Optional[str]:
    """
    Persiste los registros raw de clientes en la capa Bronze de S3.
    Devuelve la clave S3 del objeto creado (o None en caso de error).
    En LOCAL_DEV escribe en disco como antes.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename  = f"clients_{timestamp}.json"

    if os.getenv("LOCAL_DEV"):
        # ── Modo local: escribe en disco ──────────────────────────────────────
        bronze_dir = config_paths.get_bronze_path()
        os.makedirs(bronze_dir, exist_ok=True)
        full_path  = os.path.join(bronze_dir, filename)
        try:
            with open(full_path, "w", encoding="utf-8") as fh:
                json.dump(records, fh, ensure_ascii=False, indent=4)
            os.chmod(full_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            logger.info("[BRONZE] Fichero local sellado (chmod 444): %s", filename)
            return full_path
        except Exception as exc:
            logger.error("[BRONZE] Error escribiendo fichero local: %s", exc)
            return None
    else:
        # ── Modo AWS: escribe directamente en S3 ──────────────────────────────
        s3_key = f"{config_paths.get_bronze_prefix()}clients/{filename}"
        logger.info("[BRONZE] Escribiendo %d registro(s) → s3://%s/%s",
                    len(records), config_paths.S3_BUCKET, s3_key)
        ok = config_paths.write_json_to_s3(records, s3_key)
        if ok:
            logger.info("[BRONZE] Objeto S3 creado: %s", s3_key)
            return s3_key
        logger.error("[BRONZE] Error al escribir en S3: %s", s3_key)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST (S3)
# ─────────────────────────────────────────────────────────────────────────────

def _update_manifest(bronze_dir: str, path_file: str) -> None:
    """
    Actualiza el manifiesto de clientes en S3 (o en disco en LOCAL_DEV).
    """
    new_task = {
        "source":     "clients_source.xlsx",
        "path":       path_file,
        "status":     "pending",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }

    if os.getenv("LOCAL_DEV"):
        manifest_path = os.path.join(bronze_dir, "_process_manifest_clients.json")
        all_tasks: list = []
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as fh:
                    all_tasks = json.load(fh)
            except Exception:
                logger.warning("[MANIFEST] No se pudo parsear el manifiesto — empezando de cero")
        all_tasks.append(new_task)
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(all_tasks, fh, indent=4, ensure_ascii=False)
    else:
        manifest_key = f"{config_paths.get_bronze_prefix()}manifests/_process_manifest_clients.json"
        try:
            all_tasks = config_paths.read_json_from_s3(manifest_key)
        except Exception:
            all_tasks = []
        all_tasks.append(new_task)
        config_paths.write_json_to_s3(all_tasks, manifest_key)

    pending = sum(1 for t in all_tasks if t["status"] == "pending")
    logger.info("[MANIFEST] Manifiesto de clientes actualizado — tareas pendientes: %d", pending)


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def extract_clients() -> int:
    """
    Punto de entrada: extrae → ingesta en Bronze (S3) → actualiza manifiesto.
    Devuelve el número de registros ingestados (0 en caso de fallo).
    """
    logger.info("[INIT] ── extract_clients starting ──────────────────────────")

    raw_clients = extract_clients_from_excel()
    if not raw_clients:
        logger.warning("[INIT] Sin registros extraídos — abortando")
        return 0

    path_file = ingest_clients_to_bronze(raw_clients)
    if not path_file:
        logger.error("[INIT] Fallo en la ingesta Bronze — abortando")
        return 0

    bronze_dir = str(config_paths.get_bronze_path())
    _update_manifest(bronze_dir, path_file)

    total = len(raw_clients)
    logger.info("[DONE] extract_clients finished — registros ingestados: %d", total)
    return total


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    extract_clients()