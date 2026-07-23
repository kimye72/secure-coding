from market.models import Product
import os
import re
import functools

from flask import Flask, request, redirect, url_for, session, flash, render_template
from flask_socketio import send
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

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

    # 6. app.init_db 호환성 유지 (app.py에서 호출)
    def init_db():
        """ORM 스키마를 앱 컨텍스트 안에서 생성한다."""
        with app.app_context():
            db.create_all()

    app.init_db = init_db

    # 7. 라우트 등록
    _register_routes(app)

    # 8. Socket.IO 이벤트 등록
    _register_socketio_events()

    # 9. 메인 Blueprint 등록 (GET /, GET /health)
    from market.main import main_bp
    app.register_blueprint(main_bp)

    return app


# ---------------------------------------------------------------------------
# 로그인 가드 (login-required decorator)
# ---------------------------------------------------------------------------

def _get_login_required(app):
    """앱 컨텍스트에서 User 모델에 접근하는 login_required 데코레이터를 반환한다."""
    from market.models import User

    def login_required(f):
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            user_id = session.get('user_id')
            if not user_id:
                return redirect(url_for('login'))

            user = db.session.get(User, user_id)
            if user is None or not user.is_active:
                # 유효하지 않거나 비활성화된 세션 — 완전 초기화 후 로그인 페이지로
                session.clear()
                flash('세션이 만료되었거나 계정이 비활성화되었습니다. 다시 로그인해 주세요.')
                return redirect(url_for('login'))

            return f(*args, **kwargs)

        return decorated_function

    return login_required


# ---------------------------------------------------------------------------
# 라우트 등록
# ---------------------------------------------------------------------------

def _register_routes(app):
    """모든 애플리케이션 라우트를 등록한다."""
    from market.models import User, Product, Report

    login_required = _get_login_required(app)

    # -------------------------------------------------------------------
    # 인증 — 회원가입
    # -------------------------------------------------------------------

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            username = (request.form.get('username') or '').strip()
            password = request.form.get('password') or ''

            # ── 사용자명 검증 ──────────────────────────────────────────
            if len(username) < 3 or len(username) > 20:
                flash('사용자명은 3자 이상 20자 이하이어야 합니다.')
                return redirect(url_for('register'))
            if not re.fullmatch(r'[A-Za-z0-9_]+', username):
                flash('사용자명은 영문자, 숫자, 밑줄(_)만 사용할 수 있습니다.')
                return redirect(url_for('register'))

            # ── 비밀번호 검증 ──────────────────────────────────────────
            if len(password) < 8 or len(password) > 128:
                flash('비밀번호는 8자 이상 128자 이하이어야 합니다.')
                return redirect(url_for('register'))
            if not re.search(r'[A-Za-z]', password):
                flash('비밀번호에 영문자가 하나 이상 포함되어야 합니다.')
                return redirect(url_for('register'))
            if not re.search(r'[0-9]', password):
                flash('비밀번호에 숫자가 하나 이상 포함되어야 합니다.')
                return redirect(url_for('register'))

            # ── 중복 사용자명 확인 ─────────────────────────────────────
            existing = User.query.filter_by(username=username).first()
            if existing is not None:
                flash('이미 사용 중인 사용자명입니다.')
                return redirect(url_for('register'))

            # ── 사용자 생성 ────────────────────────────────────────────
            new_user = User(username=username)
            new_user.set_password(password)

            try:
                db.session.add(new_user)
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash('이미 사용 중인 사용자명입니다.')
                return redirect(url_for('register'))
            except SQLAlchemyError:
                db.session.rollback()
                flash('회원가입 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')
                return redirect(url_for('register'))

            flash('회원가입이 완료되었습니다. 로그인 해주세요.')
            return redirect(url_for('login'))

        return render_template('register.html')

    # -------------------------------------------------------------------
    # 인증 — 로그인
    # -------------------------------------------------------------------

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            username = request.form.get('username') or ''
            password = request.form.get('password') or ''

            _GENERIC_ERROR = '아이디 또는 비밀번호가 올바르지 않습니다.'

            # 사용자명으로만 조회 — 존재 여부를 외부에 노출하지 않음
            user = User.query.filter_by(username=username).first()

            if user is None or not user.is_active or not user.check_password(password):
                flash(_GENERIC_ERROR)
                return redirect(url_for('login'))

            # 세션 고정 공격 방지: 새 세션을 발급하기 전 기존 세션 초기화
            session.clear()
            session['user_id'] = user.id  # 세션에는 user_id만 저장

            flash('로그인 성공!')
            return redirect(url_for('dashboard'))

        return render_template('login.html')

    # -------------------------------------------------------------------
    # 인증 — 로그아웃
    # -------------------------------------------------------------------

    @app.route('/logout')
    def logout():
        session.clear()
        flash('로그아웃되었습니다.')
        return redirect(url_for('main.index'))

    # -------------------------------------------------------------------
    # 대시보드
    # -------------------------------------------------------------------

    @app.route('/dashboard')
    @login_required
    def dashboard():
        user = db.session.get(User, session['user_id'])
        all_products = Product.query.all()
        return render_template('dashboard.html', products=all_products, user=user)

    # -------------------------------------------------------------------
    # 프로필
    # -------------------------------------------------------------------

    @app.route('/profile', methods=['GET', 'POST'])
    @login_required
    def profile():
        user = db.session.get(User, session['user_id'])
        if request.method == 'POST':
            bio = (request.form.get('bio') or '').strip()[:300]
            user.bio = bio
            try:
                db.session.commit()
                flash('프로필이 업데이트되었습니다.')
            except SQLAlchemyError:
                db.session.rollback()
                flash('프로필 저장 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')
            return redirect(url_for('profile'))
        return render_template('profile.html', user=user)

    # -------------------------------------------------------------------
    # 새 상품 등록
    # -------------------------------------------------------------------

    @app.route('/product/new', methods=['GET', 'POST'])
    @login_required
    def new_product():
        if request.method == 'POST':
            title = (request.form.get('title') or '').strip()
            description = (request.form.get('description') or '').strip()
            price_raw = (request.form.get('price') or '').strip()

            if not title:
                flash('상품 제목을 입력해 주세요.')
                return redirect(url_for('new_product'))
            if not description:
                flash('상품 설명을 입력해 주세요.')
                return redirect(url_for('new_product'))

            try:
                price = int(price_raw)
            except (ValueError, TypeError):
                flash('가격은 정수여야 합니다.')
                return redirect(url_for('new_product'))
            if price <= 0:
                flash('가격은 0보다 커야 합니다.')
                return redirect(url_for('new_product'))

            new_prod = Product(
                title=title,
                description=description,
                price=price,
                seller_id=session['user_id'],
            )
            try:
                db.session.add(new_prod)
                db.session.commit()
                flash('상품이 등록되었습니다.')
            except SQLAlchemyError:
                db.session.rollback()
                flash('상품 등록 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')
            return redirect(url_for('dashboard'))

        return render_template('new_product.html')

    # -------------------------------------------------------------------
    # 상품 상세보기
    # -------------------------------------------------------------------

    @app.route('/product/<product_id>')
    def view_product(product_id):
        product = db.session.get(Product, product_id)
        if not product:
            flash('상품을 찾을 수 없습니다.')
            return redirect(url_for('dashboard'))
        # seller는 ORM 관계로 접근 (추가 쿼리 없음)
        seller = product.seller
        return render_template('view_product.html', product=product, seller=seller)

    # -------------------------------------------------------------------
    # 신고
    # -------------------------------------------------------------------

    @app.route('/report', methods=['GET', 'POST'])
    @login_required
    def report():
        if request.method == 'POST':
            target_type = (request.form.get('target_type') or '').strip().upper()
            target_id = (request.form.get('target_id') or '').strip()
            reason = (request.form.get('reason') or '').strip()

            valid_target_types = {Report.TARGET_USER, Report.TARGET_PRODUCT}

            if target_type not in valid_target_types:
                flash('신고 대상 유형이 올바르지 않습니다.')
                return redirect(url_for('report'))
            if not target_id:
                flash('신고 대상 ID를 입력해 주세요.')
                return redirect(url_for('report'))
            if not reason:
                flash('신고 사유를 입력해 주세요.')
                return redirect(url_for('report'))

            if target_type == Report.TARGET_USER:
                target = db.session.get(User, target_id)

            else:
                target = db.session.get(Product, target_id)

            if target is None:
                flash('신고 대상을 찾을 수 없습니다.')
                return redirect(url_for('report'))

            new_report = Report(
                reporter_id=session['user_id'],
                target_type=target_type,
                target_id=target_id,
                reason=reason,
            )
            try:
                db.session.add(new_report)
                db.session.commit()
                flash('신고가 접수되었습니다.')
            except SQLAlchemyError:
                db.session.rollback()
                flash('신고 접수 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')
            return redirect(url_for('dashboard'))

        return render_template('report.html')


# ---------------------------------------------------------------------------
# Socket.IO 이벤트 등록
# ---------------------------------------------------------------------------

def _register_socketio_events():
    """Socket.IO 이벤트 핸들러 등록"""
    import uuid

    @socketio.on('send_message')
    def handle_send_message_event(data):
        data['message_id'] = str(uuid.uuid4())
        send(data, broadcast=True)
