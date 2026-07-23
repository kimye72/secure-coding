import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "market.db"


class Config:
    # --- 기존 설정 (레거시 sqlite3 라우트에서 계속 사용) ---
    SECRET_KEY = os.environ.get('SECRET_KEY', 'secret!')
    DATABASE = os.environ.get('DATABASE', 'market.db')

    # --- SQLAlchemy 설정 ---
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'DATABASE_URL',
        f'sqlite:///{DEFAULT_DATABASE_PATH}',
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
