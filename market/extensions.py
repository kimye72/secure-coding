from flask_socketio import SocketIO

# 전역 SocketIO 객체 (아직 앱과 연결되지 않은 상태)
# create_app() 내부에서 socketio.init_app(app)으로 초기화
socketio = SocketIO()
