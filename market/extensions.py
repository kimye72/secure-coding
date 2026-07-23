import sqlite3

from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()

# 전역 SocketIO 객체 (아직 앱과 연결되지 않은 상태)
# create_app() 내부에서 socketio.init_app(app)으로 초기화
socketio = SocketIO()


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """SQLAlchemy가 관리하는 SQLite 연결에만 외래 키 제약을 활성화한다."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
