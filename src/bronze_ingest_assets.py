import json
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv

import config_paths
from logger_config import setup_logging

load_dotenv()
logger = setup_logging()

CLIENTS_S3_KEY  = "inputs/clients_source.xlsx"
ASSETS_SHEET    = "assets"

# Columnas obligatorias que debe tener la hoja
REQUIRED_COLS = [
    "client_id", "asset_id", "asset_name", "asset_type",
    "power_kw", "capacity_kwh", "is_flexible",
    "flex_window_start", "flex_window_end", "priority", "notes",
]

VALID_ASSET_TYPES = {
    "forklift_battery", "compressor", "cold_storage",
    "pump", "autoclave", "lighting", "other",
}


# ── EXTRACT ───────────────────────────────────────────────────────────────────

def extract_assets_from_excel() -> list[dict]:
    """
    Descarga clients_source.xlsx desde S3, lee la hoja 'assets'
    y devuelve los registros raw como lista de dicts.
    """
    excel_path = config_paths.get_client_path()

    logger.info(
        "[EXTRACT] Descargando Excel desde S3: s3://%s/%s",
        config_paths.S3_BUCKET, CLIENTS_S3_KEY,
    )

    ok = config_paths.download_from_s3(CLIENTS_S3_KEY, str(excel_path))
    if not ok:
        logger.error(
            "[EXTRACT] No se pudo descargar el Excel desde S3 (s3://%s/%s)",
            config_paths.S3_BUCKET, CLIENTS_S3_KEY,
        )
        return []

    try:
        import openpyxl  # noqa: F401  — comprobación explícita
    except ImportError:
        logger.error("[EXTRACT] Falta dependencia 'openpyxl' — pip install openpyxl")
        return []

    try:
        xl = pd.ExcelFile(excel_path, engine="openpyxl")
    except Exception as exc:
        logger.error("[EXTRACT] Error abriendo Excel: %s", exc)
        return []

    if ASSETS_SHEET not in xl.sheet_names:
        logger.error(
            "[EXTRACT] Hoja '%s' no encontrada en el Excel. "
            "Hojas disponibles: %s",
            ASSETS_SHEET, xl.sheet_names,
        )
        return []

    try:
        df = xl.parse(ASSETS_SHEET)
    except Exception as exc:
        logger.error("[EXTRACT] Error leyendo hoja '%s': %s", ASSETS_SHEET, exc)
        return []

    if df.empty:
        logger.warning("[EXTRACT] La hoja '%s' está vacía", ASSETS_SHEET)
        return []

    # Verificar columnas mínimas
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        logger.error(
            "[EXTRACT] Columnas obligatorias ausentes en hoja '%s': %s",
            ASSETS_SHEET, missing,
        )
        return []

    # Validar asset_type
    invalid_types = df.loc[
        ~df["asset_type"].isin(VALID_ASSET_TYPES), "asset_type"
    ].dropna().unique().tolist()
    if invalid_types:
        logger.warning(
            "[EXTRACT] Valores de asset_type no reconocidos (se mantendrán): %s. "
            "Válidos: %s", invalid_types, sorted(VALID_ASSET_TYPES),
        )

    df = df.astype(object).where(pd.notnull(df), None)
    logger.info("[EXTRACT] %d activo(s) leídos de la hoja '%s'", len(df), ASSETS_SHEET)
    return df.to_dict(orient="records")


# ── INGEST → BRONZE (S3) ──────────────────────────────────────────────────────

def ingest_assets_to_bronze(records: list[dict]) -> str | None:
    """Persiste los registros raw en Bronze (S3). Devuelve la clave S3 creada."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename  = f"assets_{timestamp}.json"
    s3_key    = f"{config_paths.get_bronze_prefix()}assets/{filename}"

    logger.info(
        "[BRONZE] Escribiendo %d registro(s) → s3://%s/%s",
        len(records), config_paths.S3_BUCKET, s3_key,
    )

    ok = config_paths.write_json_to_s3(records, s3_key)
    if ok:
        logger.info("[BRONZE] Objeto S3 creado: %s", s3_key)
        return s3_key

    logger.error("[BRONZE] Error escribiendo en S3: %s", s3_key)
    return None


# ── MANIFEST ──────────────────────────────────────────────────────────────────

def _update_manifest(path_file: str) -> None:
    manifest_key = f"{config_paths.get_bronze_prefix()}manifests/_process_manifest_assets.json"
    new_task = {
        "source":     CLIENTS_S3_KEY,
        "sheet":      ASSETS_SHEET,
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
    logger.info("[MANIFEST] Assets actualizado — pendientes: %d", pending)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def extract_assets() -> int:
    """
    Entry point del módulo.
    Devuelve el número de registros procesados (0 si fallo).
    """
    logger.info("[INIT] ── extract_assets starting ──────────────────────────")

    raw_assets = extract_assets_from_excel()
    if not raw_assets:
        logger.warning("[INIT] Sin registros extraídos — abortando")
        return 0

    path_file = ingest_assets_to_bronze(raw_assets)
    if not path_file:
        logger.error("[INIT] Fallo en ingesta Bronze — abortando")
        return 0

    _update_manifest(path_file)

    total = len(raw_assets)
    logger.info("[DONE] extract_assets finished — registros: %d", total)
    return total


if __name__ == "__main__":
    extract_assets()