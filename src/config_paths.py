"""
config_paths.py
---------------
Rutas y helpers S3.

  - Local  : S3_BUCKET y BRONZE_PREFIX vienen del .env.
  - Fargate: ECS inyecta esas mismas variables desde la task definition.
"""

import json
import os
from pathlib import Path

import boto3
from dotenv import load_dotenv

load_dotenv()

S3_BUCKET    = os.getenv("S3_BUCKET",     "sunsaver-bronze-dev")
AWS_REGION   = os.getenv("AWS_REGION",    "eu-south-2")


def get_s3_client():
    return boto3.client("s3", region_name=AWS_REGION)


def get_bronze_prefix() -> str:
    return os.getenv("BRONZE_PREFIX", "bronze/")


def get_bronze_path() -> Path:
    """Directorio temporal local para desarrollo. En Fargate usa /tmp igualmente."""
    tmp = Path("/tmp/bronze")
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


def get_client_path() -> Path:
    """Ruta temporal donde se descarga el Excel de clientes desde S3."""
    return Path("/tmp/clients_source.xlsx")


# ── Helpers S3 ────────────────────────────────────────────────────────────────

def upload_to_s3(local_path: str, s3_key: str) -> bool:
    try:
        get_s3_client().upload_file(local_path, S3_BUCKET, s3_key)
        return True
    except Exception as exc:
        print(f"[S3] Error subiendo {local_path} → s3://{S3_BUCKET}/{s3_key}: {exc}")
        return False


def download_from_s3(s3_key: str, local_path: str) -> bool:
    try:
        get_s3_client().download_file(S3_BUCKET, s3_key, local_path)
        return True
    except Exception as exc:
        print(f"[S3] Error descargando s3://{S3_BUCKET}/{s3_key}: {exc}")
        return False


def read_json_from_s3(s3_key: str) -> list | dict:
    s3  = get_s3_client()
    obj = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def write_json_to_s3(data: list | dict, s3_key: str) -> bool:
    try:
        body = json.dumps(data, ensure_ascii=False, indent=4).encode("utf-8")
        get_s3_client().put_object(Bucket=S3_BUCKET, Key=s3_key, Body=body)
        return True
    except Exception as exc:
        print(f"[S3] Error escribiendo s3://{S3_BUCKET}/{s3_key}: {exc}")
        return False


def list_s3_keys(prefix: str) -> list[str]:
    paginator = get_s3_client().get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys