"""logsink — файловый хвост логов для панели устройства (PASS 4, слой 2).

Дублирует root-логи процесса в `memory/.logs/<name>.log` (ротация, gitignored, derived).
Панель (panel.log_tail) читает хвост; docker-stdout остаётся как был. Тихий no-op при
любой ошибке — логи не должны уметь ронять процесс.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
LOGS_DIR = BASE / "memory" / ".logs"


def attach(name: str, level: int = logging.INFO) -> bool:
    """Прицепить RotatingFileHandler к root-логгеру. -> True, если получилось."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(LOGS_DIR / f"{name}.log", maxBytes=2_000_000,
                                backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        h.setLevel(level)
        logging.getLogger().addHandler(h)
        return True
    except Exception:
        logging.getLogger(__name__).debug("logsink: attach(%s) не удался", name, exc_info=True)
        return False
