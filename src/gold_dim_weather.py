import sqlalchemy
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from database_utils import get_engine
from logger_config import setup_logging


logger  = setup_logging()


# ─────────────────────────────────────────────────────────────────────────────
# BUILD
# ─────────────────────────────────────────────────────────────────────────────

def build_dim_weather(engine: sqlalchemy.engine.Engine) -> int:
    """
    Generates gold_dim_weather as a type-2 weather condition dimension,
    using a ROW_NUMBER window function to resolve duplicate weather_id entries
    by selecting the most frequently observed (main, description) pair.
    Returns the number of rows inserted.
    """
    logger.info("[INIT] ── build_dim_weather starting ───────────────────────")

    schema      = "gold"
    full_table  = f"{schema}.dim_weather"
    
    try:
        with engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
            conn.execute(text(f"DROP TABLE IF EXISTS {full_table}"))
            conn.execute(text(f"""
                CREATE TABLE {full_table} (
                    weather_id          INTEGER                  NOT NULL PRIMARY KEY,
                    weather_main        TEXT                     NOT NULL,
                    weather_description TEXT                     NOT NULL,
                    _loaded_at_utc      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
                )
            """))

            conn.execute(text(f"""
                INSERT INTO {full_table} (weather_id, weather_main, weather_description)
                SELECT
                    weather_id,
                    weather_main,
                    weather_description
                FROM (
                    SELECT
                        weather_id,
                        weather_main,
                        weather_description,
                        COUNT(*) AS freq,
                        ROW_NUMBER() OVER (
                            PARTITION BY weather_id
                            ORDER BY COUNT(*) DESC
                        ) AS rn
                    FROM silver.clean_weather
                    WHERE weather_id IS NOT NULL
                    GROUP BY weather_id, weather_main, weather_description
                ) subquery
                WHERE rn = 1
            """))

            total = conn.execute(text(f"SELECT COUNT(*) FROM {full_table}")).scalar()

        logger.info(f"[DONE] {full_table} rebuilt — rows inserted: %d", total)
        return total

    except SQLAlchemyError as exc:
        logger.error("[ERROR] SQLAlchemy error in build_dim_weather: %s", exc)
        raise
    except Exception as exc:
        logger.error("[ERROR] Unexpected error in build_dim_weather: %s", exc)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def load_dim_weather() -> int:
    """Module entry point. Returns the number of rows written (0 on failure)."""
    try:
        engine = get_engine()
        return build_dim_weather(engine)
    except Exception as exc:
        logger.critical("[ERROR] Critical failure in load_dim_weather: %s", exc)
        return 0


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    load_dim_weather()