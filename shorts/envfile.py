"""Загрузка настроек из файла .env (в корне репозитория или в папке shorts).

Переменные, уже заданные в системе, не перезаписываются.
"""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_env():
    for path in (HERE / ".env", HERE.parent / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                if value:
                    os.environ.setdefault(key.strip(), value)
    # telegram_check.py называет канал TELEGRAM_CHANNEL, publish.py — TELEGRAM_CHAT_ID
    if "TELEGRAM_CHANNEL" in os.environ:
        os.environ.setdefault("TELEGRAM_CHAT_ID", os.environ["TELEGRAM_CHANNEL"])
