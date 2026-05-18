import os
from botocore.exceptions import ClientError
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from logger_config import setup_logging
from urllib.parse import quote_plus

logger = setup_logging()

SSM_PREFIX = "/sunsaver/dev"


def _get_ssm_param(name: str) -> str:
    """Reads a SecureString parameter from AWS SSM Parameter Store."""
    client = boto3.client("ssm", region_name=os.getenv("AWS_REGION", "eu-south-2"))
    try:
        response = client.get_parameter(Name=f"{SSM_PREFIX}/{name}", WithDecryption=True)
        return response["Parameter"]["Value"]
    except ClientError as exc:
        logger.error("[DB] SSM error reading '%s': %s", name, exc)
        raise


def get_engine():
    """
    Crea y devuelve un engine de SQLAlchemy para PostgreSQL.
    Lee las credenciales desde AWS SSM Parameter Store (producción)
    o desde variables de entorno locales (desarrollo local con .env).
    """
    # ── Modo local (desarrollo): usa .env si existe ───────────────────────────
    if os.getenv("LOCAL_DEV"):
        from dotenv import load_dotenv
        load_dotenv()
        user     = os.getenv("DB_USER")
        password = os.getenv("DB_PASS")
        host     = os.getenv("DB_HOST")
        db       = os.getenv("DB_NAME")
        port     = os.getenv("DB_PORT", "5432")
        logger.info("[DB] Modo LOCAL_DEV — leyendo credenciales desde .env")
    else:
        # ── Modo AWS: lee desde SSM ───────────────────────────────────────────
        import boto3
        try:
            user     = _get_ssm_param("DB_USER")
            password = _get_ssm_param("DB_PASS")
            host     = _get_ssm_param("DB_HOST")
            db       = _get_ssm_param("DB_NAME")
            port     = _get_ssm_param("DB_PORT")
            logger.info("[DB] Credenciales cargadas desde SSM")
        except Exception as exc:
            logger.error("[DB] No se pudieron leer las credenciales de SSM: %s", exc)
            return None

    if not all([user, password, host, db]):
        logger.error("[DB] Faltan variables de conexión a la base de datos.")
        return None

    conn_url = f"postgresql://{user}:{quote_plus(password)}@{host}:{port}/{db}?sslmode=require"

    try:
        engine = create_engine(conn_url, pool_pre_ping=True)
        return engine
    except SQLAlchemyError as exc:
        logger.error("[DB] Error al crear el engine: %s", exc)
        return None