"""
database_utils.py
-----------------
Engine de SQLAlchemy para PostgreSQL.

Cómo funciona en cada entorno:
  - Local  : load_dotenv() carga el .env  → os.getenv() lee las variables.
  - Fargate: no hay .env; ECS inyecta las variables desde SSM al arrancar el contenedor.
  El código es idéntico en ambos casos.
"""

import os
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from logger_config import setup_logging

load_dotenv()
logger = setup_logging()


def get_engine():
    """
    Devuelve un engine de SQLAlchemy.
    Requiere las variables: DB_USER, DB_PASS, DB_HOST, DB_NAME, DB_PORT (opt).
    """
    user     = os.getenv("DB_USER")
    password = os.getenv("DB_PASS")
    host     = os.getenv("DB_HOST")
    db       = os.getenv("DB_NAME")
    port     = os.getenv("DB_PORT", "5432")

    if not all([user, password, host, db]):
        logger.error("[DB] Faltan variables de conexión (DB_USER, DB_PASS, DB_HOST, DB_NAME)")
        return None

    conn_url = f"postgresql://{user}:{quote_plus(password)}@{host}:{port}/{db}?sslmode=require"

    try:
        engine = create_engine(conn_url, pool_pre_ping=True)
        logger.info("[DB] Engine creado — host: %s | db: %s", host, db)
        return engine
    except SQLAlchemyError as exc:
        logger.error("[DB] Error creando engine: %s", exc)
        return None