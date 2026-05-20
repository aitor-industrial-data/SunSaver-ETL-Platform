import os
import socket
from sqlalchemy import create_engine, text, Table, Column, Integer, String, DateTime, Float, MetaData
from datetime import datetime, timezone
from dotenv import load_dotenv

from database_utils import get_engine
from logger_config import setup_logging

logger = setup_logging()

# Carga las variables del archivo .env si estás en local
load_dotenv()

metadata = MetaData()
metadata_table = Table(
    "etl_metadata", metadata,
    Column("id",                         Integer,  primary_key=True),
    Column("pipeline_name",              String),
    Column("status",                     String),
    Column("duration_seconds",           Float),
    Column("rows_affected",              Integer),
    Column("error_message",              String),
    Column("env",                        String),
    Column("_executed_by",               String),
    Column("_executed_at",               DateTime, default=lambda: datetime.now(timezone.utc)), # Corregido a lambda
    schema="etl",
)


def save_etl_metadata(status: str, duration: float, rows: int = 0, error: str = None) -> None:
    """
    Persists a pipeline execution record to the etl_metadata audit table.

    Args:
        status   : Final pipeline status string (e.g. SUCCESS, PARTIAL SUCCESS, FAILED).
        duration : Wall-clock duration in seconds.
        rows     : Total rows processed across all pipeline steps.
        error    : Human-readable error summary, or None on clean runs.
    """
    
    
    current_env = os.getenv("ENVIRONMENT", "DEV").upper()

    logger.info(
        "[METADATA] Saving audit record [%s] — status: %s | duration: %.2fs | rows: %d",
        current_env, status, duration, rows,
    )

    try:
        # 2. Detectamos quién ejecuta: Nombre del Host local o el de la tarea de AWS
        host_name = socket.gethostname()
        engine = get_engine()

        with engine.connect() as conn:
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS etl"))
            conn.commit()

        metadata.create_all(engine)

        with engine.connect() as conn:
            conn.execute(metadata_table.insert().values(
                pipeline_name   = "SunSaver_ETL",
                status          = status,
                duration_seconds= round(duration, 2),
                rows_affected   = rows,
                error_message   = error,
                env             = current_env,  # <-- Inyectamos la variable dinámica
                _executed_by    = host_name
            ))
            conn.commit()

        logger.info("[METADATA] Audit record committed successfully")

    except Exception as exc:
        logger.error("[METADATA] Failed to persist audit record: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    save_etl_metadata(status="TEST_RUN", duration=1.5, rows=100, error=None)