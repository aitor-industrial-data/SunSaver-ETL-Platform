import sqlalchemy
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from database_utils import get_engine
from logger_config import setup_logging


logger = setup_logging()


# ─────────────────────────────────────────────────────────────────────────────
# BUILD
# ─────────────────────────────────────────────────────────────────────────────

def build_dim_client(engine: sqlalchemy.engine.Engine) -> int:
    """
    Rebuilds gold_dim_client from clean_clients, deriving has_solar and
    has_battery boolean flags.  Returns the number of rows inserted.
    """
    logger.info("[INIT] ── build_dim_client starting ────────────────────────")
    
    try:
        with engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT
                    client_id, name, description, latitude, longitude, timezone,
                    nominal_load_kw, pv_peak_power_kw, panel_area_m2,
                    efficiency, panel_type, loss_pct, angle, aspect, mounting,
                    battery_capacity_kwh, soc_min_pct, installation_cost_eur
                FROM silver.clean_clients
                ORDER BY client_id
            """)).fetchall()

        if not rows:
            logger.warning("[EXTRACT] clean_clients is empty — gold_dim_client not generated")
            return 0

        logger.info("[EXTRACT] %d client(s) read from clean_clients", len(rows))

        registros = [
            {
                "client_id":             r.client_id,
                "name":                  r.name,
                "description":           r.description,
                "latitude":              r.latitude,
                "longitude":             r.longitude,
                "timezone":              r.timezone,
                "nominal_load_kw":       r.nominal_load_kw,
                "pv_peak_power_kw":      r.pv_peak_power_kw,
                "panel_area_m2":         r.panel_area_m2,
                "efficiency":            r.efficiency,
                "panel_type":            r.panel_type,
                "loss_pct":              r.loss_pct,
                "angle":                 r.angle,
                "aspect":                r.aspect,
                "mounting":              r.mounting,
                "battery_capacity_kwh":  r.battery_capacity_kwh,
                "soc_min_pct":           r.soc_min_pct,
                "installation_cost_eur": r.installation_cost_eur,
                "has_solar":             1 if (r.pv_peak_power_kw or 0) > 0 else 0,
                "has_battery":           1 if (r.battery_capacity_kwh or 0) > 0 else 0,
            }
            for r in rows
        ]

        logger.info("[TRANSFORM] Derived flags computed for %d client(s)", len(registros))

        schema      = "gold"
        full_table  = f"{schema}.dim_client"

        with engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
            conn.execute(text(f"DROP TABLE IF EXISTS {full_table}"))
            conn.execute(text(f"""
                CREATE TABLE {full_table} (
                    client_id               TEXT    PRIMARY KEY,
                    name                    TEXT    NOT NULL,
                    description             TEXT,
                    latitude                DOUBLE PRECISION NOT NULL,
                    longitude               DOUBLE PRECISION NOT NULL,
                    timezone                TEXT    NOT NULL,
                    nominal_load_kw         DOUBLE PRECISION NOT NULL,
                    pv_peak_power_kw        DOUBLE PRECISION NOT NULL,
                    panel_area_m2           DOUBLE PRECISION NOT NULL,
                    efficiency              DOUBLE PRECISION NOT NULL,
                    panel_type              TEXT    NOT NULL,
                    loss_pct                DOUBLE PRECISION NOT NULL,
                    angle                   DOUBLE PRECISION NOT NULL,
                    aspect                  DOUBLE PRECISION NOT NULL,
                    mounting                TEXT    NOT NULL,
                    battery_capacity_kwh    DOUBLE PRECISION NOT NULL,
                    soc_min_pct             DOUBLE PRECISION NOT NULL,
                    installation_cost_eur   DOUBLE PRECISION NOT NULL,
                    has_solar               INTEGER NOT NULL,
                    has_battery             INTEGER NOT NULL,
                    _loaded_at_utc          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
                )
            """))
            conn.execute(text(f"""
                INSERT INTO {full_table} (
                    client_id, name, description, latitude, longitude, timezone,
                    nominal_load_kw, pv_peak_power_kw, panel_area_m2,
                    efficiency, panel_type, loss_pct, angle, aspect, mounting,
                    battery_capacity_kwh, soc_min_pct, installation_cost_eur,
                    has_solar, has_battery
                ) VALUES (
                    :client_id, :name, :description, :latitude, :longitude, :timezone,
                    :nominal_load_kw, :pv_peak_power_kw, :panel_area_m2,
                    :efficiency, :panel_type, :loss_pct, :angle, :aspect, :mounting,
                    :battery_capacity_kwh, :soc_min_pct, :installation_cost_eur,
                    :has_solar, :has_battery
                )
            """), registros)

        total = len(registros)
        logger.info(f"[DONE] {full_table} rebuilt — rows inserted: %d", total)
        return total

    except SQLAlchemyError as exc:
        logger.error("[ERROR] SQLAlchemy error in build_dim_client: %s", exc)
        raise
    except Exception as exc:
        logger.error("[ERROR] Unexpected error in build_dim_client: %s", exc)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def load_dim_client() -> int:
    """Module entry point. Returns the number of rows written (0 on failure)."""
    try:
        engine = get_engine()
        return build_dim_client(engine)
    except Exception as exc:
        logger.critical("[ERROR] Critical failure in load_dim_client: %s", exc)
        return 0


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    load_dim_client()