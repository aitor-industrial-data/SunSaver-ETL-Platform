import os
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from dotenv import load_dotenv
from logger_config import setup_logging

logger = setup_logging()
load_dotenv()

def get_engine():
    """Crea y devuelve un engine de SQLAlchemy para PostgreSQL."""
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASS")
    host = os.getenv("DB_HOST")
    db = os.getenv("DB_NAME")
    port = os.getenv("DB_PORT", "5432")
    
    # Verificación de seguridad
    if not all([user, password, host, db]):
        logger.error("[DB] Faltan variables de entorno para la conexión a la base de datos.")
        return None

    conn_url = f"postgresql://{user}:{password}@{host}:{port}/{db}"
    
    try:
        # pool_pre_ping comprueba si la conexión sigue viva antes de usarla
        engine = create_engine(conn_url, pool_pre_ping=True)
        return engine
    except SQLAlchemyError as e:
        logger.error(f"[DB] Error al crear el engine: {e}")
        return None

