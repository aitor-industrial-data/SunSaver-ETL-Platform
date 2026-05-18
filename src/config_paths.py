import os
import boto3
from pathlib import Path
from dotenv import load_dotenv


# ── Configuración S3 ──────────────────────────────────────────────────────────
S3_BUCKET  = os.getenv("S3_BUCKET", "sunsaver-bronze-dev")
AWS_REGION = os.getenv("AWS_REGION", "eu-south-2")


def get_s3_client():
    """Devuelve un cliente boto3 de S3."""
    return boto3.client("s3", region_name=AWS_REGION)


def get_bronze_prefix() -> str:
    """
    Devuelve el prefijo S3 que actúa como directorio Bronze.
    Equivalente al antiguo get_bronze_path() pero para S3.
    """
    return os.getenv("BRONZE_PREFIX", "bronze/")


def get_bronze_path() -> Path:
    """
    Compatibilidad con el código original que usa Path local.
    En AWS Lambda usamos /tmp como directorio temporal.
    Los ficheros se suben a S3 inmediatamente tras escribirse.
    """
    tmp_bronze = Path("/tmp/bronze")
    tmp_bronze.mkdir(parents=True, exist_ok=True)
    return tmp_bronze


def get_db_path() -> Path:
    """Mantenido por compatibilidad — en AWS usamos RDS, no SQLite."""
    load_dotenv()
    BASE_DIR  = Path(__file__).resolve().parent.parent
    _default  = BASE_DIR / "data" / "sunsaver.db"
    _env_path = os.getenv("DB_PATH")
    final     = Path(_env_path) if _env_path else _default
    final.parent.mkdir(parents=True, exist_ok=True)
    return final.resolve()


def get_client_path() -> Path:
    """
    En AWS el Excel de clientes se lee desde S3.
    Esta función devuelve la ruta temporal donde se descarga en Lambda.
    """
    tmp_path = Path("/tmp/clients_source.xlsx")
    return tmp_path


# ── Helpers S3 ────────────────────────────────────────────────────────────────

def upload_to_s3(local_path: str, s3_key: str) -> bool:
    """Sube un fichero local a S3 y devuelve True si tiene éxito."""
    try:
        s3 = get_s3_client()
        s3.upload_file(local_path, S3_BUCKET, s3_key)
        return True
    except Exception as exc:
        print(f"[S3] Error subiendo {local_path} → s3://{S3_BUCKET}/{s3_key}: {exc}")
        return False


def download_from_s3(s3_key: str, local_path: str) -> bool:
    """Descarga un objeto S3 a una ruta local y devuelve True si tiene éxito."""
    try:
        s3 = get_s3_client()
        s3.download_file(S3_BUCKET, s3_key, local_path)
        return True
    except Exception as exc:
        print(f"[S3] Error descargando s3://{S3_BUCKET}/{s3_key}: {exc}")
        return False


def read_json_from_s3(s3_key: str) -> dict:
    """Lee y parsea un JSON directamente desde S3 sin escribir a disco."""
    import json
    s3  = get_s3_client()
    obj = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def write_json_to_s3(data: dict, s3_key: str) -> bool:
    """Serializa un dict como JSON y lo escribe directamente en S3."""
    import json
    try:
        s3   = get_s3_client()
        body = json.dumps(data, ensure_ascii=False, indent=4).encode("utf-8")
        s3.put_object(Bucket=S3_BUCKET, Key=s3_key, Body=body)
        return True
    except Exception as exc:
        print(f"[S3] Error escribiendo s3://{S3_BUCKET}/{s3_key}: {exc}")
        return False


def list_s3_keys(prefix: str) -> list[str]:
    """Lista todos los objetos S3 bajo un prefijo dado."""
    s3       = get_s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    keys     = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys