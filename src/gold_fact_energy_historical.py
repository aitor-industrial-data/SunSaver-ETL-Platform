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

def build_fact_energy_historical(engine: sqlalchemy.engine.Engine) -> int:
    """
    Incrementally upserts past rows into gold.fact_energy_historical.

    Fuentes:
        · Previsión + weather → gold.fact_energy_forecast  (unix_time < now)
        · Contexto D-1        → silver.clean_context       (pivot inline)
        · PVPC real D-1       → silver.clean_prices        (price_type = 'PVPC')

    Debe ejecutarse ANTES de gold_fact_energy_forecast.py (que trunca la tabla
    de previsión), para que los datos de ayer aún estén disponibles.

    Returns the number of rows affected.
    """
    logger.info("[INIT] ── build_fact_energy_historical starting ──────────────")

    now_unix   = int(datetime.now(timezone.utc).timestamp())
    schema     = "gold"
    full_table = f"{schema}.fact_energy_historical"

    logger.info("[EXTRACT] Reading forecast rows with unix_time < %d", now_unix)

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
                    demand_real_mw          DOUBLE PRECISION,
                    pv_gen_mw               DOUBLE PRECISION,
                    co2_tco2_mw             DOUBLE PRECISION,
                    upward_imb              DOUBLE PRECISION,
                    _loaded_at_utc          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                    PRIMARY KEY (client_id, unix_time)
                )
            """))

            # ── 2. Índices ────────────────────────────────────────────────────
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS idx_gold_hist_unix_time "
                f"ON {full_table} (unix_time)"
            ))
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS idx_gold_hist_weather_id "
                f"ON {full_table} (weather_id)"
            ))

            # ── 3. Upsert ─────────────────────────────────────────────────────
            result = conn.execute(text(f"""
                INSERT INTO {full_table} (
                    client_id, unix_time, forecast_time_utc,
                    pv_power_gen_kw, pv_performance_ratio, poa_wm2,
                    t_cell_celsius, power_consumption_kw,
                    temp_celsius, humidity_pct, clouds_pct,
                    rain_prob_norm, wind_speed_mps, weather_id,
                    price_pvpc_eur_mwh,
                    demand_real_mw, pv_gen_mw, co2_tco2_mw, upward_imb
                )
                SELECT
                    f.client_id,
                    f.unix_time,
                    f.forecast_time_utc,
                    f.pv_power_gen_kw,
                    f.pv_performance_ratio,
                    f.poa_wm2,
                    f.t_cell_celsius,
                    f.power_consumption_kw,
                    f.temp_celsius,
                    f.humidity_pct,
                    f.clouds_pct,
                    f.rain_prob_norm,
                    f.wind_speed_mps,
                    f.weather_id,
                    pvpc.price_euro_mwh                         AS price_pvpc_eur_mwh,
                    ctx.demand_real_mw,
                    ctx.pv_gen_mw,
                    ctx.co2_tco2_mw,
                    ctx.upward_imb
                FROM gold.fact_energy_forecast f

                LEFT JOIN silver.clean_prices pvpc
                    ON  pvpc.unix_time  = f.unix_time
                    AND pvpc.price_type = 'PVPC'

                LEFT JOIN (
                    SELECT
                        unix_time,
                        MAX(CASE WHEN indicator_name = 'demand_real' THEN value END) AS demand_real_mw,
                        MAX(CASE WHEN indicator_name = 'pv_gen'      THEN value END) AS pv_gen_mw,
                        MAX(CASE WHEN indicator_name = 'co2'         THEN value END) AS co2_tco2_mw,
                        MAX(CASE WHEN indicator_name = 'upward_imb'  THEN value END) AS upward_imb
                    FROM silver.clean_context
                    GROUP BY unix_time
                ) ctx ON ctx.unix_time = f.unix_time

                WHERE f.unix_time < :now_unix

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
                    demand_real_mw       = EXCLUDED.demand_real_mw,
                    pv_gen_mw            = EXCLUDED.pv_gen_mw,
                    co2_tco2_mw          = EXCLUDED.co2_tco2_mw,
                    upward_imb           = EXCLUDED.upward_imb,
                    _loaded_at_utc       = now()
            """), {"now_unix": now_unix})

            rows_affected = result.rowcount

        logger.info(
            "[DONE] %s updated — rows upserted: %d (unix_time < %d)",
            full_table, rows_affected, now_unix,
        )
        return rows_affected

    except SQLAlchemyError as exc:
        logger.error("[ERROR] SQLAlchemy error in build_fact_energy_historical: %s", exc)
        raise
    except Exception as exc:
        logger.error("[ERROR] Unexpected error in build_fact_energy_historical: %s", exc)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def load_fact_energy_historical() -> int:
    """Module entry point. Returns the number of rows upserted (0 on failure)."""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            pass
        return build_fact_energy_historical(engine)
    except Exception as exc:
        logger.critical("[ERROR] Critical failure in load_fact_energy_historical: %s", exc)
        return 0


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    load_fact_energy_historical()
