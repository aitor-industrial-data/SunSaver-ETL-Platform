import logging
import sys
from pathlib import Path
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()


def setup_logging() -> logging.Logger:
    """
    Configura logging para Fargate (stdout → CloudWatch) y local (archivo).
    En AWS Fargate todo va a stdout para que CloudWatch lo capture.
    """

    logger = logging.getLogger("SunSaver")
    logger.setLevel(logging.DEBUG)

    if logger.handlers:                     # Idempotente: no duplicar handlers
        return logger

    fmt = logging.Formatter(
        fmt     = "%(asctime)s | %(levelname)-8s | %(module)-30s | %(message)s",
        datefmt = "%Y-%m-%d %H:%M:%S",
    )

    # ── Console handler (Fargate → CloudWatch Logs) ─────────────────────────
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    
    # ── File handler solo en LOCAL_DEV ────────────────────────────────────────
    if os.getenv("LOCAL_DEV"):
        BASE_DIR = Path(__file__).resolve().parent.parent
        log_dir  = BASE_DIR / "logs"
        print(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"sunsaver_{datetime.now().strftime('%Y-%m-%d')}.log"
        fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        

    return logger


if __name__ == "__main__":
    setup_logging()
