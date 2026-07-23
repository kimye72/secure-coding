import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "market.db"


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'secret!')

    # --- SQLAlchemy 설정 ---
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'DATABASE_URL',
        f'sqlite:///{DEFAULT_DATABASE_PATH}',
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- 세션 쿠키 보안 설정 ---
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
