import sqlalchemy
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime, timezone

from database_utils import get_engine
from logger_config import setup_logging


logger = setup_logging()


# ─────────────────────────────────────────────────────────────────────────────
# BUILD
# ─────────────────────────────────────────────────────────────────────────────

def build_fact_energy_forecast(engine: sqlalchemy.engine.Engine) -> int:
    """
    Rebuilds gold.fact_energy_forecast from scratch with only future rows.

    Fuentes:
        · silver.clean_calculations  (previsión de generación y consumo)
        · silver.clean_weather        (variables meteorológicas)
        · silver.clean_prices         (PVPC D+1, price_type = 'PVPC')

    Solo filas con unix_time >= now. Sin columnas de contexto peninsular
    (éstas viven exclusivamente en gold.fact_energy_historical).

    Estrategia: TRUNCATE + INSERT en cada ejecución.
    Debe ejecutarse DESPUÉS de gold_fact_energy_historical.py.

    Returns the number of rows inserted.
    """
    logger.info("[INIT] ── build_fact_energy_forecast starting ──────────────")

    now_unix   = int(datetime.now(timezone.utc).timestamp())
    schema     = "gold"
    full_table = f"{schema}.fact_energy_forecast"

    logger.info("[EXTRACT] Building forecast window: unix_time >= %d", now_unix)

    try:
        with engine.begin() as conn:

            # ── 1. DDL ────────────────────────────────────────────────────────
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {full_table} (
                    client_id               TEXT             NOT NULL,
                    unix_time               BIGINT           NOT NULL,
                    forecast_time_utc       TIMESTAMP WITH TIME ZONE NOT NULL,
                    pv_power_gen_kw         DOUBLE PRECISION,
                    pv_performance_ratio    DOUBLE PRECISION,
                    poa_wm2                 DOUBLE PRECISION,
                    t_cell_celsius          DOUBLE PRECISION,
                    power_consumption_kw    DOUBLE PRECISION,
                    temp_celsius            DOUBLE PRECISION,
                    humidity_pct            DOUBLE PRECISION,
                    clouds_pct              DOUBLE PRECISION,
                    rain_prob_norm          DOUBLE PRECISION,
                    wind_speed_mps          DOUBLE PRECISION,
                    weather_id              INTEGER,
                    price_pvpc_eur_mwh      DOUBLE PRECISION,
                    _loaded_at_utc          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    PRIMARY KEY (client_id, unix_time)
                )
            """))

            # ── 2. Índices ────────────────────────────────────────────────────
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS idx_gold_fcast_unix_time "
                f"ON {full_table} (unix_time)"
            ))
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS idx_gold_fcast_weather_id "
                f"ON {full_table} (weather_id)"
            ))

            # ── 3. TRUNCATE + INSERT ──────────────────────────────────────────
            conn.execute(text(f"TRUNCATE TABLE {full_table}"))
            logger.info("[TRUNCATE] %s cleared", full_table)

            result = conn.execute(text(f"""
                INSERT INTO {full_table} (
                    client_id, unix_time, forecast_time_utc,
                    pv_power_gen_kw, pv_performance_ratio, poa_wm2,
                    t_cell_celsius, power_consumption_kw,
                    temp_celsius, humidity_pct, clouds_pct,
                    rain_prob_norm, wind_speed_mps, weather_id,
                    price_pvpc_eur_mwh
                )
                SELECT
                    c.client_id,
                    c.unix_time,
                    c.forecast_time_utc,
                    c.pv_power_gen_kw,
                    c.pv_performance_ratio,
                    c.poa_wm2,
                    c.t_cell_celsius,
                    c.power_con_kw                              AS power_consumption_kw,
                    w.temp_celsius,
                    w.humidity_pct,
                    w.clouds_pct,
                    w.rain_prob_norm,
                    w.wind_speed_mps,
                    w.weather_id,
                    pvpc.price_euro_mwh                         AS price_pvpc_eur_mwh
                FROM silver.clean_calculations c

                LEFT JOIN silver.clean_weather w
                    ON  w.client_id = c.client_id
                    AND w.unix_time = c.unix_time

                LEFT JOIN silver.clean_prices pvpc
                    ON  pvpc.unix_time  = c.unix_time
                    AND pvpc.price_type = 'PVPC'

                WHERE c.unix_time >= :now_unix
            """), {"now_unix": now_unix})

            rows_inserted = result.rowcount

        logger.info(
            "[DONE] %s rebuilt — rows inserted: %d (unix_time >= %d)",
            full_table, rows_inserted, now_unix,
        )
        return rows_inserted

    except SQLAlchemyError as exc:
        logger.error("[ERROR] SQLAlchemy error in build_fact_energy_forecast: %s", exc)
        raise
    except Exception as exc:
        logger.error("[ERROR] Unexpected error in build_fact_energy_forecast: %s", exc)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def load_fact_energy_forecast() -> int:
    """Module entry point. Returns the number of rows inserted (0 on failure)."""
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
