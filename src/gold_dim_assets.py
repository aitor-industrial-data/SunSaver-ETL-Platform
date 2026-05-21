import sqlalchemy
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from database_utils import get_engine
from logger_config import setup_logging

logger = setup_logging()


# ── BUILD ─────────────────────────────────────────────────────────────────────

def build_dim_assets(engine: sqlalchemy.engine.Engine) -> int:
    """
    Rebuilds gold.dim_assets from silver.clean_assets, deriving
    has_capacity and is_overnight_flexible boolean flags.
    Returns the number of rows inserted.
    """
    logger.info("[INIT] ── build_dim_assets starting ────────────────────────")

    try:
        with engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT
                    asset_id, client_id, asset_name, asset_type,
                    power_kw, capacity_kwh, is_flexible,
                    flex_window_start, flex_window_end, priority, notes
                FROM silver.clean_assets
                ORDER BY client_id, priority, asset_id
            """)).fetchall()

        if not rows:
            logger.warning("[EXTRACT] clean_assets está vacía — gold.dim_assets no generada")
            return 0

        logger.info("[EXTRACT] %d activo(s) leídos de clean_assets", len(rows))

        registros = [
            {
                "asset_id":               r.asset_id,
                "client_id":              r.client_id,
                "asset_name":             r.asset_name,
                "asset_type":             r.asset_type,
                "power_kw":               r.power_kw,
                "capacity_kwh":           r.capacity_kwh,
                "is_flexible":            r.is_flexible,
                "flex_window_start":      r.flex_window_start,
                "flex_window_end":        r.flex_window_end,
                "priority":               r.priority,
                "notes":                  r.notes,
                # Flag: tiene capacidad de almacenamiento (batería, frío, depósito)
                "has_capacity":           1 if (r.capacity_kwh or 0) > 0 else 0,
                # Flag: ventana flexible cubre horas nocturnas (0–6)
                "is_overnight_flexible":  1 if (
                    r.is_flexible == 1
                    and (r.flex_window_start or 0) <= 2
                    and (r.flex_window_end   or 0) >= 5
                ) else 0,
            }
            for r in rows
        ]

        logger.info("[TRANSFORM] Flags derivados calculados para %d activo(s)", len(registros))

        schema     = "gold"
        full_table = f"{schema}.dim_assets"

        with engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
            conn.execute(text(f"DROP TABLE IF EXISTS {full_table}"))
            conn.execute(text(f"""
                CREATE TABLE {full_table} (
                    asset_id                TEXT    NOT NULL PRIMARY KEY,
                    client_id               TEXT    NOT NULL,
                    asset_name              TEXT    NOT NULL,
                    asset_type              TEXT    NOT NULL,
                    power_kw                REAL    NOT NULL,
                    capacity_kwh            REAL    NOT NULL DEFAULT 0,
                    is_flexible             INTEGER NOT NULL DEFAULT 0,
                    flex_window_start       INTEGER NOT NULL DEFAULT 0,
                    flex_window_end         INTEGER NOT NULL DEFAULT 23,
                    priority                INTEGER NOT NULL DEFAULT 99,
                    notes                   TEXT    NOT NULL DEFAULT '',
                    has_capacity            INTEGER NOT NULL DEFAULT 0,
                    is_overnight_flexible   INTEGER NOT NULL DEFAULT 0,
                    _loaded_at_utc          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
                )
            """))
            conn.execute(text(f"""
                INSERT INTO {full_table} (
                    asset_id, client_id, asset_name, asset_type,
                    power_kw, capacity_kwh, is_flexible,
                    flex_window_start, flex_window_end, priority, notes,
                    has_capacity, is_overnight_flexible
                ) VALUES (
                    :asset_id, :client_id, :asset_name, :asset_type,
                    :power_kw, :capacity_kwh, :is_flexible,
                    :flex_window_start, :flex_window_end, :priority, :notes,
                    :has_capacity, :is_overnight_flexible
                )
            """), registros)

        total = len(registros)
        logger.info("[DONE] %s reconstruida — filas insertadas: %d", full_table, total)
        return total

    except SQLAlchemyError as exc:
        logger.error("[ERROR] SQLAlchemy error en build_dim_assets: %s", exc)
        raise
    except Exception as exc:
        logger.error("[ERROR] Error inesperado en build_dim_assets: %s", exc)
        raise


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def load_dim_assets() -> int:
    """Entry point del módulo. Devuelve el número de filas escritas (0 si fallo)."""
    try:
        engine = get_engine()
        return build_dim_assets(engine)
    except Exception as exc:
        logger.critical("[ERROR] Fallo crítico en load_dim_assets: %s", exc)
        return 0


if __name__ == "__main__":
    load_dim_assets()