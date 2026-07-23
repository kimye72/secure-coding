import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "market.db"


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY')
    PRODUCT_IMAGE_MAX_BYTES = 5 * 1024 * 1024
    MAX_CONTENT_LENGTH = 6 * 1024 * 1024

    # --- SQLAlchemy 설정 ---
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'DATABASE_URL',
        f'sqlite:///{DEFAULT_DATABASE_PATH}',
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- 세션 쿠키 보안 설정 ---
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = (
        os.environ.get('SESSION_COOKIE_SECURE', 'false').strip().lower()
        in {'1', 'true', 'yes', 'on'}
    )
