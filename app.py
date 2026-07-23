from market import create_app
from market.extensions import socketio

app = create_app()

if __name__ == '__main__':
    app.init_db()  # 최초 실행 시 테이블 생성
    socketio.run(app, debug=app.config.get("DEBUG", False))
