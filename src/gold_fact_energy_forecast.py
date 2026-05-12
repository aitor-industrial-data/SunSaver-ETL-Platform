import sqlalchemy
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime, timezone

from database_utils import get_engine
from logger_config import setup_logging


logger  = setup_logging()



# ─────────────────────────────────────────────────────────────────────────────
# BUILD
# ─────────────────────────────────────────────────────────────────────────────

def build_fact_energy_forecast(engine: sqlalchemy.engine.Engine) -> int:
    """
    Incrementally upserts the Gold fact table by joining the active forecast
    window (unix_time >= now - 2h) from clean_calculations with clean_weather
    and clean_prices.  Returns the number of rows affected.
    """
    logger.info("[INIT] ── build_fact_energy_forecast starting ──────────────")

    buffer_seconds = 7200
    start_unix     = int(datetime.now(timezone.utc).timestamp()) - buffer_seconds

    logger.info("[EXTRACT] Active window lower bound: unix_time >= %d (now - %ds)", start_unix, buffer_seconds)

    try:
        with engine.begin() as conn:
            # 1. Crear tabla con tus nombres exactos
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS gold_fact_energy_forecast (
                    client_id               TEXT    NOT NULL,
                    unix_time               INTEGER NOT NULL,
                    forecast_time_utc       TEXT    NOT NULL,
                    pv_power_gen_kw         REAL,
                    pv_performance_ratio    REAL,
                    poa_wm2                 REAL,
                    t_cell_celsius          REAL,
                    power_consumption_kw    REAL,
                    temp_celsius            REAL,
                    humidity_pct            REAL,
                    clouds_pct              REAL,
                    rain_prob_norm          REAL,
                    wind_speed_mps          REAL,
                    weather_id              INTEGER,
                    price_pvpc_eur_mwh      REAL,
                    _loaded_at_utc          TEXT    NOT NULL,
                    PRIMARY KEY (client_id, unix_time)
                )
            """))

            # 2. Insert con sintaxis PostgreSQL (ON CONFLICT) y nombres exactos
            result = conn.execute(text("""
                INSERT INTO gold_fact_energy_forecast (
                    client_id, unix_time, forecast_time_utc, pv_power_gen_kw,
                    pv_performance_ratio, poa_wm2, t_cell_celsius, power_consumption_kw,
                    temp_celsius, humidity_pct, clouds_pct, rain_prob_norm,
                    wind_speed_mps, weather_id, price_pvpc_eur_mwh, _loaded_at_utc
                )
                SELECT
                    c.client_id,
                    c.unix_time,
                    c.forecast_time_utc,
                    c.pv_power_gen_kw,
                    c.pv_performance_ratio,
                    c.poa_wm2,
                    c.t_cell_celsius,
                    c.power_con_kw,
                    w.temp_celsius,
                    w.humidity_pct,
                    w.clouds_pct,
                    w.rain_prob_norm,
                    w.wind_speed_mps,
                    w.weather_id,
                    pvpc.price_euro_mwh             AS price_pvpc_eur_mwh,
                    TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS') AS _loaded_at_utc
                FROM clean_calculations c
                LEFT JOIN clean_weather w
                    ON  w.client_id = c.client_id
                    AND w.unix_time = c.unix_time
                LEFT JOIN clean_prices pvpc
                    ON  pvpc.unix_time  = c.unix_time
                    AND pvpc.price_type = 'PVPC'
                WHERE c.unix_time >= :start_unix
                ON CONFLICT (client_id, unix_time)
                DO UPDATE SET
                    forecast_time_utc    = EXCLUDED.forecast_time_utc,
                    pv_power_gen_kw      = EXCLUDED.pv_power_gen_kw,
                    pv_performance_ratio = EXCLUDED.pv_performance_ratio,
                    poa_wm2              = EXCLUDED.poa_wm2,
                    t_cell_celsius       = EXCLUDED.t_cell_celsius,
                    power_consumption_kw = EXCLUDED.power_consumption_kw,
                    temp_celsius         = EXCLUDED.temp_celsius,
                    humidity_pct         = EXCLUDED.humidity_pct,
                    clouds_pct           = EXCLUDED.clouds_pct,
                    rain_prob_norm       = EXCLUDED.rain_prob_norm,
                    wind_speed_mps       = EXCLUDED.wind_speed_mps,
                    weather_id           = EXCLUDED.weather_id,
                    price_pvpc_eur_mwh   = EXCLUDED.price_pvpc_eur_mwh,
                    _loaded_at_utc       = EXCLUDED._loaded_at_utc
            """), {"start_unix": start_unix})

            rows_affected = result.rowcount

            # 3. Índices (Sintaxis estándar)
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_gold_fact_unix_time  "
                "ON gold_fact_energy_forecast (unix_time)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_gold_fact_weather_id "
                "ON gold_fact_energy_forecast (weather_id)"
            ))

        logger.info(
            "[DONE] gold_fact_energy_forecast updated — rows upserted: %d (window start: %d)",
            rows_affected, start_unix,
        )
        return rows_affected

    except SQLAlchemyError as exc:
        logger.error("[ERROR] SQLAlchemy error in build_fact_energy_forecast: %s", exc)
        raise
    except Exception as exc:
        logger.error("[ERROR] Unexpected error in build_fact_energy_forecast: %s", exc)
        raise
        

# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def load_fact_energy_forecast() -> int:
    """Module entry point. Returns the number of rows upserted (0 on failure)."""
    try:
        engine = get_engine()

        with engine.connect() as conn:
            pass 
            
        return build_fact_energy_forecast(engine)
    except Exception as exc:
        logger.critical("[ERROR] Critical failure in load_fact_energy_forecast: %s", exc)
        return 0


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    load_fact_energy_forecast()
