import os
import sqlite3

from flask import Flask, request, redirect, url_for, session, flash, render_template, g
from flask_socketio import send

from market.config import Config
from market.extensions import db, socketio


def create_app(test_config=None):
    """Flask 애플리케이션 팩토리

    Args:
        test_config: 테스트 시 적용할 설정 dict (예: {'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:'})
                     None이면 market.config.Config를 사용한다.
    """
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'templates'),
        static_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
        if os.path.isdir(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static'))
        else None,
    )

    # 1. 기본 설정 로드
    app.config.from_object(Config)

    # 2. 테스트 설정 적용 (test_config가 제공된 경우 기본 설정을 덮어씀)
    if test_config is not None:
        app.config.update(test_config)

    # 3. SQLAlchemy 초기화 (db.create_all()은 여기서 호출하지 않음)
    db.init_app(app)

    # 4. SocketIO 초기화
    socketio.init_app(app)

    # 5. ORM 메타데이터 등록 (순환 import 방지를 위해 create_app 내부에서 import)
    import market.models  # noqa: F401

    # 6. 레거시 sqlite3 데이터베이스 헬퍼 등록
    _register_db(app)

    # 7. 기존 라우트 등록 (이후 단계에서 Blueprint로 분리)
    _register_legacy_routes(app)

    # 8. Socket.IO 이벤트 등록
    _register_socketio_events()

    # 9. 메인 Blueprint 등록 (GET /, GET /health)
    from market.main import main_bp
    app.register_blueprint(main_bp)

    return app


def _register_db(app):
    """데이터베이스 연결 관리 등록"""

    def get_db():
        db = getattr(g, '_database', None)
        if db is None:
            db = g._database = sqlite3.connect(app.config['DATABASE'])
            db.row_factory = sqlite3.Row
        return db

    # get_db를 앱 컨텍스트에서 접근 가능하도록 app에 저장
    app.get_db = get_db

    @app.teardown_appcontext
    def close_connection(exception):
        db = getattr(g, '_database', None)
        if db is not None:
            db.close()

    def init_db():
        with app.app_context():
            db = get_db()
            cursor = db.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    bio TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS product (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    price TEXT NOT NULL,
                    seller_id TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS report (
                    id TEXT PRIMARY KEY,
                    reporter_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    reason TEXT NOT NULL
                )
            """)
            db.commit()

    app.init_db = init_db


def _register_legacy_routes(app):
    """기존 라우트를 이후 단계에서 Blueprint로 분리하기 전까지 직접 등록"""
    import uuid

    def get_db():
        return app.get_db()

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            username = request.form['username']
            password = request.form['password']
            db = get_db()
            cursor = db.cursor()
            cursor.execute("SELECT * FROM user WHERE username = ?", (username,))
            if cursor.fetchone() is not None:
                flash('이미 존재하는 사용자명입니다.')
                return redirect(url_for('register'))
            user_id = str(uuid.uuid4())
            cursor.execute("INSERT INTO user (id, username, password) VALUES (?, ?, ?)",
                           (user_id, username, password))
            db.commit()
            flash('회원가입이 완료되었습니다. 로그인 해주세요.')
            return redirect(url_for('login'))
        return render_template('register.html')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            username = request.form['username']
            password = request.form['password']
            db = get_db()
            cursor = db.cursor()
            cursor.execute("SELECT * FROM user WHERE username = ? AND password = ?", (username, password))
            user = cursor.fetchone()
            if user:
                session['user_id'] = user['id']
                flash('로그인 성공!')
                return redirect(url_for('dashboard'))
            else:
                flash('아이디 또는 비밀번호가 올바르지 않습니다.')
                return redirect(url_for('login'))
        return render_template('login.html')

    @app.route('/logout')
    def logout():
        session.pop('user_id', None)
        flash('로그아웃되었습니다.')
        return redirect(url_for('main.index'))

    @app.route('/dashboard')
    def dashboard():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
        current_user = cursor.fetchone()
        cursor.execute("SELECT * FROM product")
        all_products = cursor.fetchall()
        return render_template('dashboard.html', products=all_products, user=current_user)

    @app.route('/profile', methods=['GET', 'POST'])
    def profile():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        db = get_db()
        cursor = db.cursor()
        if request.method == 'POST':
            bio = request.form.get('bio', '')
            cursor.execute("UPDATE user SET bio = ? WHERE id = ?", (bio, session['user_id']))
            db.commit()
            flash('프로필이 업데이트되었습니다.')
            return redirect(url_for('profile'))
        cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
        current_user = cursor.fetchone()
        return render_template('profile.html', user=current_user)

    @app.route('/product/new', methods=['GET', 'POST'])
    def new_product():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if request.method == 'POST':
            title = request.form['title']
            description = request.form['description']
            price = request.form['price']
            db = get_db()
            cursor = db.cursor()
            product_id = str(uuid.uuid4())
            cursor.execute(
                "INSERT INTO product (id, title, description, price, seller_id) VALUES (?, ?, ?, ?, ?)",
                (product_id, title, description, price, session['user_id'])
            )
            db.commit()
            flash('상품이 등록되었습니다.')
            return redirect(url_for('dashboard'))
        return render_template('new_product.html')

    @app.route('/product/<product_id>')
    def view_product(product_id):
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM product WHERE id = ?", (product_id,))
        product = cursor.fetchone()
        if not product:
            flash('상품을 찾을 수 없습니다.')
            return redirect(url_for('dashboard'))
        cursor.execute("SELECT * FROM user WHERE id = ?", (product['seller_id'],))
        seller = cursor.fetchone()
        return render_template('view_product.html', product=product, seller=seller)

    @app.route('/report', methods=['GET', 'POST'])
    def report():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if request.method == 'POST':
            target_id = request.form['target_id']
            reason = request.form['reason']
            db = get_db()
            cursor = db.cursor()
            report_id = str(uuid.uuid4())
            cursor.execute(
                "INSERT INTO report (id, reporter_id, target_id, reason) VALUES (?, ?, ?, ?)",
                (report_id, session['user_id'], target_id, reason)
            )
            db.commit()
            flash('신고가 접수되었습니다.')
            return redirect(url_for('dashboard'))
        return render_template('report.html')


def _register_socketio_events():
    """Socket.IO 이벤트 핸들러 등록"""
    import uuid

    @socketio.on('send_message')
    def handle_send_message_event(data):
        data['message_id'] = str(uuid.uuid4())
        send(data, broadcast=True)
