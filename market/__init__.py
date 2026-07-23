from market.models import Product
import os
import re
import uuid
import functools
import unicodedata

from datetime import datetime, timezone

from flask import Flask, abort, request, redirect, send_file, url_for, session, flash, render_template
from flask_socketio import emit, join_room, leave_room
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import RequestEntityTooLarge

from market.config import Config
from market.extensions import db, socketio, csrf, limiter
from market.product_images import (
    MAX_PRODUCT_IMAGE_BYTES,
    ProductImageError,
    backup_existing_product_images,
    cleanup_staged_product_image,
    discard_product_image_backups,
    get_product_image,
    has_product_image_upload,
    install_staged_product_image,
    product_image_exists,
    remove_file_if_exists,
    remove_product_images,
    restore_product_image_backups,
    stage_product_image,
)
from flask_limiter.util import get_remote_address


def create_app(test_config=None):
    """Flask 애플리케이션 팩토리

    Args:
        test_config: 테스트 시 적용할 설정 dict (예: {'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:'})\n                     None이면 market.config.Config를 사용한다.
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

    if not app.config.get('SECRET_KEY'):
        raise RuntimeError('SECRET_KEY 환경변수가 설정되지 않았습니다.')

    app.config.setdefault('PRODUCT_IMAGE_MAX_BYTES', MAX_PRODUCT_IMAGE_BYTES)
    app.config.setdefault(
        'PRODUCT_IMAGE_UPLOAD_DIR',
        os.path.join(app.instance_path, 'uploads', 'products'),
    )

    # 3. SQLAlchemy 초기화 (db.create_all()은 여기서 호출하지 않음)
    db.init_app(app)

    csrf.init_app(app)

    app.config.setdefault('RATELIMIT_STORAGE_URI', 'memory://')
    app.config.setdefault('RATELIMIT_HEADERS_ENABLED', True)
    # memory storage is for the current development/classroom environment and does not persist across process restarts.
    limiter.init_app(app)

    # 4. Socket.IO 이벤트 핸들러 등록 — socketio.init_app()보다 먼저 호출해야 한다
    _register_socketio_events()

    # 5. SocketIO 초기화
    socketio.init_app(app)

    # 6. ORM 메타데이터 등록 (순환 import 방지를 위해 create_app 내부에서 import)
    import market.models  # noqa: F401

    # 7. app.init_db 호환성 유지 (app.py에서 호출)
    def init_db():
        """ORM 스키마를 앱 컨텍스트 안에서 생성한다."""
        with app.app_context():
            db.create_all()

    app.init_db = init_db

    # 8. 라우트 등록
    _register_routes(app)

    # 9. 메인 Blueprint 등록 (GET /, GET /health)
    from market.main import main_bp
    app.register_blueprint(main_bp)

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        return render_template('csrf_error.html'), 400

    @app.errorhandler(429)
    def handle_rate_limit_error(e):
        return render_template('rate_limit_error.html'), 429

    @app.errorhandler(RequestEntityTooLarge)
    def handle_request_entity_too_large(e):
        return '업로드 파일이 너무 큽니다.', 413

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        return response

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
# 관리자 권한 가드 (admin-required decorator)
# ---------------------------------------------------------------------------

def _get_admin_required(app):
    """앱 컨텍스트에서 User 모델에 접근하는 admin_required 데코레이터를 반환한다."""
    from market.models import User

    def admin_required(f):
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            user_id = session.get('user_id')
            if not user_id:
                flash('권한이 없습니다.')
                return redirect(url_for('login'))

            user = db.session.get(User, user_id)
            if user is None or not user.is_active or user.role != User.ROLE_ADMIN:
                flash('권한이 없습니다.')
                return redirect(url_for('dashboard'))

            return f(*args, **kwargs)

        return decorated_function

    return admin_required


# ---------------------------------------------------------------------------
# Rate-limit identity helper
# ---------------------------------------------------------------------------

def _rate_limit_identity():
    user_id = session.get('user_id')
    if user_id:
        return f'user:{user_id}'
    return f'ip:{get_remote_address()}'


# ---------------------------------------------------------------------------
# 공유 상품 입력 유효성 검사 헬퍼
# ---------------------------------------------------------------------------

def _validate_product_fields(form):
    """상품 폼 필드(title, description, price)를 검증하고 (title, description, price, error) 튜플을 반환한다.

    규칙:
    - title: 필수, strip 후 최대 100자
    - description: 필수, strip 후 최대 2,000자
    - price: 필수 양의 정수

    오류가 없으면 error는 None이다.
    seller_id는 절대로 폼에서 읽지 않는다.
    """
    title = (form.get('title') or '').strip()
    description = (form.get('description') or '').strip()
    price_raw = (form.get('price') or '').strip()

    if not title:
        return None, None, None, '상품 제목을 입력해 주세요.'
    if len(title) > 100:
        return None, None, None, '상품 제목은 100자 이하이어야 합니다.'

    if not description:
        return None, None, None, '상품 설명을 입력해 주세요.'
    if len(description) > 2000:
        return None, None, None, '상품 설명은 2,000자 이하이어야 합니다.'

    try:
        price = int(price_raw)
    except (ValueError, TypeError):
        return None, None, None, '가격은 정수여야 합니다.'
    if price <= 0:
        return None, None, None, '가격은 0보다 커야 합니다.'

    return title, description, price, None


def _normalize_trade_location(value):
    """사용자가 입력한 거래 지역을 검증하고 정규화한다.

    빈 값은 None으로 저장한다. GPS 좌표, IP 기반 위치, 자동 추론값은
    사용하지 않으며 폼의 단일 텍스트 필드만 처리한다.
    """
    location = (value or '').strip()
    if not location:
        return None, None

    if len(location) > 120:
        return None, '거래 지역은 120자 이하로 입력해 주세요.'

    for char in location:
        if unicodedata.category(char).startswith('C'):
            return None, '거래 지역을 올바르게 입력해 주세요.'

    return location, None


def _normalize_location_filter(value):
    """대시보드 위치 필터를 안전하게 정규화한다. 부적절한 값은 무시한다."""
    location, error = _normalize_trade_location(value)
    if error:
        return ''
    return location or ''


def _like_contains_pattern(value):
    escaped = (
        value
        .replace('\\', '\\\\')
        .replace('%', '\\%')
        .replace('_', '\\_')
    )
    return f'%{escaped}%'


def _validate_review_fields(form):
    rating_raw = (form.get('rating') or '').strip()
    try:
        rating = int(rating_raw)
    except (ValueError, TypeError):
        return None, None, '평점을 올바르게 선택해 주세요.'

    if rating < 1 or rating > 5:
        return None, None, '평점은 1점에서 5점 사이여야 합니다.'

    content = (form.get('content') or '').strip()
    if not content:
        return rating, None, None
    if len(content) > 500:
        return None, None, '리뷰 내용은 500자 이하로 입력해 주세요.'
    for char in content:
        if unicodedata.category(char).startswith('C'):
            return None, None, '리뷰 내용을 올바르게 입력해 주세요.'

    return rating, content, None


# ---------------------------------------------------------------------------
# 채팅 권한 검증 헬퍼
# ---------------------------------------------------------------------------

def _authorize_trade_chat(trade_id, user_id):
    """trade_id와 user_id를 검증하고 (user, trade, product)를 반환한다.

    아래 열 가지 조건이 모두 충족돼야 성공한다:
    1. user_id 존재
    2. User 존재
    3. User.is_active == True
    4. Trade 존재
    5. Product 존재
    6. Trade.product_id == Product.id
    7. Trade.seller_id == Product.seller_id
    8. user.id == Trade.buyer_id 또는 Trade.seller_id
    9. Trade.status == ACCEPTED
    10. Product.status == RESERVED

    실패 시 (None, None, None)을 반환한다. 어느 조건이 실패했는지 노출하지 않는다.
    """
    from market.models import User, Trade, Product

    if not user_id or not trade_id:
        return None, None, None

    try:
        user = db.session.get(User, user_id)
        if user is None or not user.is_active:
            return None, None, None

        trade = db.session.get(Trade, trade_id)
        if trade is None:
            return None, None, None

        product = db.session.get(Product, trade.product_id)
        if product is None:
            return None, None, None

        # 참조 무결성 교차 확인
        if trade.product_id != product.id:
            return None, None, None
        if trade.seller_id != product.seller_id:
            return None, None, None

        # 구매자 또는 판매자인지 확인
        if user.id not in (trade.buyer_id, trade.seller_id):
            return None, None, None

        # 상태 확인
        if trade.status != Trade.STATUS_ACCEPTED:
            return None, None, None
        if product.status != Product.STATUS_RESERVED:
            return None, None, None

        return user, trade, product

    except SQLAlchemyError:
        db.session.rollback()
        return None, None, None


# ---------------------------------------------------------------------------
# UUID 정규화 헬퍼
# ---------------------------------------------------------------------------

def _normalize_trade_id(value):
    """value를 UUID 문자열로 정규화한다.

    유효한 UUID이면 str(uuid.UUID(...))를 반환한다.
    유효하지 않으면 None을 반환한다.
    리스트, dict, None, 빈 문자열, 비UUID 형식 값은 모두 거부한다.
    UUID 파싱 세부 사항은 노출하지 않는다.
    """
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None

def _resolve_report_target(target_type, target_id):
    """target_type과 target_id를 검증하고 대상을 반환한다.
    실패 시 None을 반환하며, 오류 상세를 노출하지 않는다."""
    from market.models import User, Product, Report

    if target_type not in (Report.TARGET_USER, Report.TARGET_PRODUCT):
        return None

    try:
        str(uuid.UUID(str(target_id)))
    except (ValueError, TypeError, AttributeError):
        return None

    try:
        if target_type == Report.TARGET_USER:
            return db.session.get(User, target_id)
        elif target_type == Report.TARGET_PRODUCT:
            return db.session.get(Product, target_id)
    except SQLAlchemyError:
        db.session.rollback()
    return None


# ---------------------------------------------------------------------------
# 라우트 등록
# ---------------------------------------------------------------------------

def _register_routes(app):
    """모든 애플리케이션 라우트를 등록한다."""
    from market.models import User, Product, Trade, Review, Report

    login_required = _get_login_required(app)

    trade_state_action_limit = limiter.shared_limit('30 per hour', scope='trade-state-actions', key_func=_rate_limit_identity, methods=['POST'])
    trade_completion_action_limit = limiter.shared_limit('20 per hour', scope='trade-completion-actions', key_func=_rate_limit_identity, methods=['POST'])
    review_action_limit = limiter.shared_limit('10 per hour', scope='review-actions', key_func=_rate_limit_identity, methods=['POST'])
    admin_report_action_limit = limiter.shared_limit('30 per hour', scope='admin-report-actions', key_func=_rate_limit_identity, methods=['POST'])
    admin_user_action_limit = limiter.shared_limit('30 per hour', scope='admin-user-actions', key_func=_rate_limit_identity, methods=['POST'])

    # 허용된 상품 상태 집합
    _VALID_STATUSES = {Product.STATUS_SELLING, Product.STATUS_RESERVED, Product.STATUS_SOLD}

    # 편집/삭제 시 공통으로 사용하는 권한 없음 메시지
    _PRODUCT_AUTH_ERROR = '상품을 찾을 수 없거나 수정 권한이 없습니다.'

    def _review_context(trade_id, reviewer_id):
        validated_trade_id = _normalize_trade_id(trade_id)
        if validated_trade_id is None:
            return None

        trade = db.session.get(Trade, validated_trade_id)
        if trade is None or trade.status != Trade.STATUS_COMPLETED:
            return None

        product = db.session.get(Product, trade.product_id)
        if product is None or product.id != trade.product_id or product.seller_id != trade.seller_id:
            return None

        if reviewer_id == trade.buyer_id:
            reviewee_id = trade.seller_id
        elif reviewer_id == trade.seller_id:
            reviewee_id = trade.buyer_id
        else:
            return None

        if reviewee_id == reviewer_id:
            return None

        reviewee = db.session.get(User, reviewee_id)
        if reviewee is None:
            return None

        existing_review = Review.query.filter_by(
            trade_id=trade.id,
            reviewer_id=reviewer_id,
        ).first()

        return {
            'trade': trade,
            'product': product,
            'reviewee': reviewee,
            'existing_review': existing_review,
        }

    # -------------------------------------------------------------------
    # 인증 — 회원가입
    # -------------------------------------------------------------------

    @app.route('/register', methods=['GET', 'POST'])
    @limiter.limit('3 per hour', methods=['POST'])
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
    @limiter.limit('5 per minute;20 per hour', methods=['POST'])
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

    @app.route('/logout', methods=['POST'])
    def logout():
        session.clear()
        flash('로그아웃되었습니다.')
        return redirect(url_for('login'))

    # -------------------------------------------------------------------
    # 대시보드 — 상품 목록 + 검색/필터
    # -------------------------------------------------------------------

    @app.route('/dashboard')
    @login_required
    def dashboard():
        user = db.session.get(User, session['user_id'])

        # ── 검색어 처리 ────────────────────────────────────────────────
        query_raw = (request.args.get('q') or '').strip()
        query = query_raw[:100]  # 최대 100자로 제한
        location_query = _normalize_location_filter(request.args.get('location'))

        # ── 상태 필터 처리 ─────────────────────────────────────────────
        status_param = (request.args.get('status') or '').strip().upper()
        selected_status = status_param if status_param in _VALID_STATUSES else ''

        # ── ORM 쿼리 구성 ──────────────────────────────────────────────
        # SQLAlchemy ORM 쿼리만 사용; 문자열 SQL 없음
        q = Product.query

        if query:
            # contains()는 내부적으로 LIKE 바인딩 파라미터를 사용하므로 SQL 인젝션 안전
            # autoescape=True는 %, _ 같은 LIKE 메타문자를 이스케이프함
            search_filter = db.or_(
                Product.title.contains(query, autoescape=True),
                Product.description.contains(query, autoescape=True),
            )
            q = q.filter(search_filter)

        if selected_status:
            q = q.filter(Product.status == selected_status)

        if location_query:
            q = q.filter(Product.trade_location.ilike(_like_contains_pattern(location_query), escape='\\'))

        products = q.order_by(Product.created_at.desc()).all()
        product_image_ids = {
            product.id
            for product in products
            if product_image_exists(app.config['PRODUCT_IMAGE_UPLOAD_DIR'], product.id)
        }

        return render_template(
            'dashboard.html',
            user=user,
            products=products,
            product_image_ids=product_image_ids,
            query=query,
            location_query=location_query,
            selected_status=selected_status,
            product_statuses=list(_VALID_STATUSES),
        )

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
    # 마이페이지 — 내가 등록한 상품 관리
    # -------------------------------------------------------------------

    @app.route('/mypage')
    @login_required
    def mypage():
        current_user_id = session['user_id']
        user = db.session.get(User, current_user_id)

        products = (
            Product.query
            .filter(Product.seller_id == current_user_id)
            .order_by(Product.created_at.desc(), Product.id.asc())
            .all()
        )
        product_image_ids = {
            product.id
            for product in products
            if product_image_exists(app.config['PRODUCT_IMAGE_UPLOAD_DIR'], product.id)
        }

        return render_template(
            'mypage.html',
            user=user,
            products=products,
            product_image_ids=product_image_ids,
        )

    # -------------------------------------------------------------------
    # 새 상품 등록 (강화된 버전)
    # -------------------------------------------------------------------

    @app.route('/product/new', methods=['GET', 'POST'])
    @limiter.limit('10 per hour', methods=['POST'], key_func=_rate_limit_identity)
    @login_required
    def new_product():
        if request.method == 'POST':
            # 공유 유효성 검사 헬퍼 사용 — seller_id는 절대로 폼에서 읽지 않음
            title, description, price, error = _validate_product_fields(request.form)
            if error:
                flash(error)
                return redirect(url_for('new_product'))
            trade_location, location_error = _normalize_trade_location(request.form.get('trade_location'))
            if location_error:
                flash(location_error)
                return redirect(url_for('new_product'))

            image_file = request.files.get('image')
            staged_image = None
            installed_image_path = None

            try:
                new_prod = Product(
                    title=title,
                    description=description,
                    price=price,
                    trade_location=trade_location,
                    seller_id=session['user_id'],          # 항상 세션에서 설정
                    status=Product.STATUS_SELLING,          # 초기 상태는 항상 SELLING
                )
                db.session.add(new_prod)

                if has_product_image_upload(image_file):
                    db.session.flush()
                    staged_image = stage_product_image(
                        image_file,
                        new_prod.id,
                        app.config['PRODUCT_IMAGE_UPLOAD_DIR'],
                        app.config['PRODUCT_IMAGE_MAX_BYTES'],
                    )
                    install_staged_product_image(staged_image)
                    installed_image_path = staged_image.final_path

                db.session.commit()
                flash('상품이 등록되었습니다.')
                return redirect(url_for('dashboard'))
            except ProductImageError:
                db.session.rollback()
                cleanup_staged_product_image(staged_image)
                if installed_image_path is not None:
                    remove_file_if_exists(installed_image_path)
                flash('상품 등록 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')
                return redirect(url_for('new_product'))
            except SQLAlchemyError:
                db.session.rollback()
                cleanup_staged_product_image(staged_image)
                if installed_image_path is not None:
                    remove_file_if_exists(installed_image_path)
                flash('상품 등록 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')
                return redirect(url_for('new_product'))

        return render_template('new_product.html')

    # -------------------------------------------------------------------
    # 상품 이미지 — GET /product/<product_id>/image
    # -------------------------------------------------------------------

    @app.route('/product/<product_id>/image')
    def product_image(product_id):
        try:
            product_uuid = str(uuid.UUID(str(product_id)))
        except (ValueError, TypeError, AttributeError):
            abort(404)

        product = db.session.get(Product, product_uuid)
        if product is None:
            abort(404)

        image = get_product_image(app.config['PRODUCT_IMAGE_UPLOAD_DIR'], product.id)
        if image is None:
            abort(404)

        response = send_file(
            image.path,
            mimetype=image.mime_type,
            as_attachment=False,
            download_name=f'product-{product.id}.{image.extension}',
            conditional=True,
        )
        response.headers['Content-Disposition'] = (
            f'inline; filename="product-{product.id}.{image.extension}"'
        )
        return response

    # -------------------------------------------------------------------
    # 상품 상세보기
    # -------------------------------------------------------------------

    @app.route('/product/<product_id>')
    def view_product(product_id):
        product = db.session.get(Product, product_id)
        if not product:
            flash('상품을 찾을 수 없습니다.')
            return redirect(url_for('dashboard'))

        # seller는 ORM 관계로 접근
        seller = product.seller

        # 소유권은 서버에서 결정 — 클라이언트 입력에 의존하지 않음
        current_user_id = session.get('user_id')
        is_owner = (
            current_user_id is not None
            and current_user_id == product.seller_id
        )

        # 현재 로그인한 구매자의 활성 거래 요청 조회 (판매자 자신은 제외)
        active_trade = None
        can_request_trade = False
        if current_user_id and not is_owner:
            active_trade = (
                Trade.query
                .filter(
                    Trade.product_id == product.id,
                    Trade.buyer_id == current_user_id,
                    Trade.status.in_([Trade.STATUS_PENDING, Trade.STATUS_ACCEPTED]),
                )
                .first()
            )
            can_request_trade = (
                product.status == Product.STATUS_SELLING
                and active_trade is None
            )

        product_reviews = (
            Review.query
            .filter(Review.product_id == product.id)
            .order_by(Review.created_at.desc(), Review.id.asc())
            .all()
        )
        product_average_rating = None
        if product_reviews:
            product_average_rating = sum(review.rating for review in product_reviews) / len(product_reviews)

        return render_template(
            'view_product.html',
            product=product,
            seller=seller,
            is_owner=is_owner,
            current_user_id=current_user_id,
            active_trade=active_trade,
            can_request_trade=can_request_trade,
            product_has_image=product_image_exists(app.config['PRODUCT_IMAGE_UPLOAD_DIR'], product.id),
            product_reviews=product_reviews,
            product_average_rating=product_average_rating,
        )

    # -------------------------------------------------------------------
    # 사용자 리뷰 — GET /user/<user_id>/reviews
    # -------------------------------------------------------------------

    @app.route('/user/<user_id>/reviews')
    def user_reviews(user_id):
        try:
            target_user_id = str(uuid.UUID(str(user_id)))
        except (ValueError, TypeError, AttributeError):
            abort(404)

        target_user = db.session.get(User, target_user_id)
        if target_user is None:
            abort(404)

        reviews = (
            Review.query
            .filter(Review.reviewee_id == target_user.id)
            .order_by(Review.created_at.desc(), Review.id.asc())
            .all()
        )
        average_rating = None
        if reviews:
            average_rating = sum(review.rating for review in reviews) / len(reviews)

        return render_template(
            'user_reviews.html',
            target_user=target_user,
            reviews=reviews,
            average_rating=average_rating,
        )

    # -------------------------------------------------------------------
    # 상품 수정 — GET/POST /product/<product_id>/edit
    # -------------------------------------------------------------------

    @app.route('/product/<product_id>/edit', methods=['GET', 'POST'])
    @login_required
    def edit_product(product_id):
        product = db.session.get(Product, product_id)

        # 상품 미존재 또는 비소유자: 동일한 제네릭 메시지로 정보 노출 방지
        if product is None or product.seller_id != session['user_id']:
            flash(_PRODUCT_AUTH_ERROR)
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            # ── 필드 유효성 검사 ────────────────────────────────────────
            title, description, price, error = _validate_product_fields(request.form)
            if error:
                flash(error)
                return redirect(url_for('edit_product', product_id=product_id))
            trade_location, location_error = _normalize_trade_location(request.form.get('trade_location'))
            if location_error:
                flash(location_error)
                return redirect(url_for('edit_product', product_id=product_id))

            # ── 상태 유효성 검사 ─────────────────────────────────────────
            status = (request.form.get('status') or '').strip().upper()
            if status not in _VALID_STATUSES:
                flash('올바르지 않은 상품 상태입니다.')
                return redirect(url_for('edit_product', product_id=product_id))

            accepted_trade = Trade.query.filter(
                Trade.product_id == product.id,
                Trade.status == Trade.STATUS_ACCEPTED
            ).first()

            if accepted_trade:
                if status == Product.STATUS_SELLING or (product.status == Product.STATUS_SOLD and status != Product.STATUS_SOLD):
                    flash('수락된 거래가 있는 상품은 거래를 취소하지 않고 판매 중으로 변경할 수 없습니다.')
                    return redirect(url_for('edit_product', product_id=product_id))
            else:
                pending_trade = Trade.query.filter(
                    Trade.product_id == product.id,
                    Trade.status == Trade.STATUS_PENDING
                ).first()
                if pending_trade:
                    if status != Product.STATUS_SELLING:
                        flash('대기 중인 거래 요청이 있는 상품의 상태는 직접 변경할 수 없습니다.')
                        return redirect(url_for('edit_product', product_id=product_id))

            image_file = request.files.get('image')
            staged_image = None
            image_backups = []
            installed_image_path = None

            try:
                if has_product_image_upload(image_file):
                    staged_image = stage_product_image(
                        image_file,
                        product.id,
                        app.config['PRODUCT_IMAGE_UPLOAD_DIR'],
                        app.config['PRODUCT_IMAGE_MAX_BYTES'],
                    )
                    image_backups = backup_existing_product_images(
                        app.config['PRODUCT_IMAGE_UPLOAD_DIR'],
                        product.id,
                    )
                    install_staged_product_image(staged_image)
                    installed_image_path = staged_image.final_path

                # ── 안전한 필드만 업데이트 (seller_id 절대 변경하지 않음) ──
                product.title = title
                product.description = description
                product.price = price
                product.trade_location = trade_location
                product.status = status

                db.session.commit()
                discard_product_image_backups(image_backups)
                flash('상품이 수정되었습니다.')
                return redirect(url_for('view_product', product_id=product_id))
            except ProductImageError:
                db.session.rollback()
                cleanup_staged_product_image(staged_image)
                if installed_image_path is not None:
                    remove_file_if_exists(installed_image_path)
                restore_product_image_backups(image_backups)
                flash('상품 수정 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')
                return redirect(url_for('edit_product', product_id=product_id))
            except SQLAlchemyError:
                db.session.rollback()
                cleanup_staged_product_image(staged_image)
                if installed_image_path is not None:
                    remove_file_if_exists(installed_image_path)
                restore_product_image_backups(image_backups)
                flash('상품 수정 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')
                return redirect(url_for('edit_product', product_id=product_id))

        # GET: 기존 값으로 폼 사전 채움
        return render_template(
            'edit_product.html',
            product=product,
            product_has_image=product_image_exists(app.config['PRODUCT_IMAGE_UPLOAD_DIR'], product.id),
        )

    # -------------------------------------------------------------------
    # 상품 삭제 — POST /product/<product_id>/delete
    # -------------------------------------------------------------------

    @app.route('/product/<product_id>/delete', methods=['POST'])
    @login_required
    def delete_product(product_id):
        product = db.session.get(Product, product_id)

        # 상품 미존재 또는 비소유자: 동일한 제네릭 메시지로 정보 노출 방지
        if product is None or product.seller_id != session['user_id']:
            flash(_PRODUCT_AUTH_ERROR)
            return redirect(url_for('dashboard'))

        # 거래 기록이 있는 상품은 삭제 불가
        if product.trades:
            flash('거래 기록이 있는 상품은 삭제할 수 없습니다.')
            return redirect(url_for('view_product', product_id=product_id))

        try:
            product_id_to_delete = product.id
            db.session.delete(product)
            db.session.commit()
            remove_product_images(app.config['PRODUCT_IMAGE_UPLOAD_DIR'], product_id_to_delete)
            flash('상품이 삭제되었습니다.')
        except SQLAlchemyError:
            db.session.rollback()
            flash('상품 삭제 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')

        return redirect(url_for('dashboard'))

    # -------------------------------------------------------------------
    # 거래 요청 생성 — POST /product/<product_id>/trade/request
    # -------------------------------------------------------------------

    @app.route('/product/<product_id>/trade/request', methods=['POST'])
    @limiter.limit('10 per hour', methods=['POST'], key_func=_rate_limit_identity)
    @login_required
    def trade_request(product_id):
        current_user_id = session['user_id']
        product = db.session.get(Product, product_id)

        # 상품 미존재, 자기 상품, 판매 중이 아닌 상품, 중복 요청을 모두 제네릭 메시지로 처리
        _TRADE_REQ_ERROR = '거래 요청을 처리할 수 없습니다.'

        if product is None:
            flash(_TRADE_REQ_ERROR)
            return redirect(url_for('dashboard'))

        # 자기 자신 상품에 요청 불가
        if product.seller_id == current_user_id:
            flash(_TRADE_REQ_ERROR)
            return redirect(url_for('view_product', product_id=product_id))

        # 판매 중인 상품만 요청 가능
        if product.status != Product.STATUS_SELLING:
            flash(_TRADE_REQ_ERROR)
            return redirect(url_for('view_product', product_id=product_id))

        # 동일 상품에 대해 이미 PENDING 또는 ACCEPTED 요청이 있으면 거부
        existing = (
            Trade.query
            .filter(
                Trade.product_id == product.id,
                Trade.buyer_id == current_user_id,
                Trade.status.in_([Trade.STATUS_PENDING, Trade.STATUS_ACCEPTED]),
            )
            .first()
        )
        if existing is not None:
            flash(_TRADE_REQ_ERROR)
            return redirect(url_for('view_product', product_id=product_id))

        # 모든 검증 통과 — buyer_id/seller_id는 세션과 ORM에서만 설정
        new_trade = Trade(
            product_id=product.id,
            buyer_id=current_user_id,       # 폼에서 읽지 않음
            seller_id=product.seller_id,    # 폼에서 읽지 않음
            status=Trade.STATUS_PENDING,
        )
        try:
            db.session.add(new_trade)
            db.session.commit()
            flash('거래 요청이 전송되었습니다.')
        except SQLAlchemyError:
            db.session.rollback()
            flash('거래 요청 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')

        return redirect(url_for('view_product', product_id=product_id))

    # -------------------------------------------------------------------
    # 거래 내역 — GET /trades
    # -------------------------------------------------------------------

    @app.route('/trades')
    @login_required
    def trades():
        current_user_id = session['user_id']

        # 발신 거래 (현재 사용자가 구매자인 거래), 최신순
        outgoing_trades = (
            Trade.query
            .filter(Trade.buyer_id == current_user_id)
            .order_by(Trade.created_at.desc())
            .all()
        )

        # 수신 거래 (현재 사용자가 판매자인 거래), 최신순
        incoming_trades = (
            Trade.query
            .filter(Trade.seller_id == current_user_id)
            .order_by(Trade.created_at.desc())
            .all()
        )

        visible_trade_ids = [trade.id for trade in outgoing_trades + incoming_trades]
        reviewed_trade_ids = set()
        if visible_trade_ids:
            reviewed_trade_ids = {
                review.trade_id
                for review in Review.query.filter(
                    Review.reviewer_id == current_user_id,
                    Review.trade_id.in_(visible_trade_ids),
                ).all()
            }

        return render_template(
            'trades.html',
            outgoing_trades=outgoing_trades,
            incoming_trades=incoming_trades,
            current_user_id=current_user_id,
            reviewed_trade_ids=reviewed_trade_ids,
        )

    # -------------------------------------------------------------------
    # 거래 수락 — POST /trade/<trade_id>/accept
    # -------------------------------------------------------------------

    @app.route('/trade/<trade_id>/accept', methods=['POST'])
    @trade_state_action_limit
    @login_required
    def trade_accept(trade_id):
        current_user_id = session['user_id']
        trade = db.session.get(Trade, trade_id)

        _TRADE_AUTH_ERROR = '거래를 처리할 수 없습니다.'

        # 거래 미존재 또는 현재 사용자가 판매자가 아닌 경우
        if trade is None or trade.seller_id != current_user_id:
            flash(_TRADE_AUTH_ERROR)
            return redirect(url_for('trades'))

        # 거래 상태가 PENDING이어야 함
        if trade.status != Trade.STATUS_PENDING:
            flash(_TRADE_AUTH_ERROR)
            return redirect(url_for('trades'))

        # 상품 존재 및 소유권 재확인
        product = db.session.get(Product, trade.product_id)
        if product is None or product.seller_id != current_user_id:
            flash(_TRADE_AUTH_ERROR)
            return redirect(url_for('trades'))

        # 상품이 아직 판매 중이어야 함
        if product.status != Product.STATUS_SELLING:
            flash(_TRADE_AUTH_ERROR)
            return redirect(url_for('trades'))

        # 다른 수락된 거래가 있는지 확인
        existing_accepted = Trade.query.filter(
            Trade.product_id == product.id,
            Trade.status == Trade.STATUS_ACCEPTED,
            Trade.id != trade.id
        ).first()

        if existing_accepted is not None:
            flash(_TRADE_AUTH_ERROR)
            return redirect(url_for('trades'))

        try:
            # 1. 선택된 거래 수락
            trade.status = Trade.STATUS_ACCEPTED
            # 2. 상품 상태를 예약으로 변경
            product.status = Product.STATUS_RESERVED
            # 3. 동일 상품의 다른 PENDING 거래를 모두 거절
            competing = (
                Trade.query
                .filter(
                    Trade.product_id == product.id,
                    Trade.status == Trade.STATUS_PENDING,
                    Trade.id != trade.id,
                )
                .all()
            )
            for other in competing:
                other.status = Trade.STATUS_REJECTED
            # 한 번만 커밋
            db.session.commit()
            flash('거래 요청을 수락했습니다.')
        except SQLAlchemyError:
            db.session.rollback()
            flash('거래 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')

        return redirect(url_for('trades'))

    # -------------------------------------------------------------------
    # 거래 거절 — POST /trade/<trade_id>/reject
    # -------------------------------------------------------------------

    @app.route('/trade/<trade_id>/reject', methods=['POST'])
    @trade_state_action_limit
    @login_required
    def trade_reject(trade_id):
        current_user_id = session['user_id']
        trade = db.session.get(Trade, trade_id)

        _TRADE_AUTH_ERROR = '거래를 처리할 수 없습니다.'

        # 거래 미존재 또는 현재 사용자가 판매자가 아닌 경우 (구매자의 거절 차단)
        if trade is None or trade.seller_id != current_user_id:
            flash(_TRADE_AUTH_ERROR)
            return redirect(url_for('trades'))

        # 거래 상태가 PENDING이어야 함 (REJECTED는 종료 상태)
        if trade.status != Trade.STATUS_PENDING:
            flash(_TRADE_AUTH_ERROR)
            return redirect(url_for('trades'))

        # 상품 소유권 재확인
        product = db.session.get(Product, trade.product_id)
        if product is None or product.seller_id != current_user_id:
            flash(_TRADE_AUTH_ERROR)
            return redirect(url_for('trades'))

        try:
            trade.status = Trade.STATUS_REJECTED
            # 상품 상태는 변경하지 않음
            db.session.commit()
            flash('거래 요청을 거절했습니다.')
        except SQLAlchemyError:
            db.session.rollback()
            flash('거래 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')

        return redirect(url_for('trades'))

    # -------------------------------------------------------------------
    # 거래 취소 — POST /trade/<trade_id>/cancel
    # -------------------------------------------------------------------

    @app.route('/trade/<trade_id>/cancel', methods=['POST'])
    @trade_state_action_limit
    @login_required
    def trade_cancel(trade_id):
        current_user_id = session['user_id']
        trade = db.session.get(Trade, trade_id)

        _TRADE_AUTH_ERROR = '거래를 처리할 수 없습니다.'

        # 거래 미존재 또는 무관한 사용자
        if trade is None or (
            trade.buyer_id != current_user_id
            and trade.seller_id != current_user_id
        ):
            flash(_TRADE_AUTH_ERROR)
            return redirect(url_for('trades'))

        product = db.session.get(Product, trade.product_id)

        if trade.status == Trade.STATUS_PENDING:
            # PENDING 취소: 구매자만 가능
            if trade.buyer_id != current_user_id:
                flash(_TRADE_AUTH_ERROR)
                return redirect(url_for('trades'))
            try:
                trade.status = Trade.STATUS_CANCELLED
                # 상품 상태 변경 없음
                db.session.commit()
                flash('거래 요청이 취소되었습니다.')
            except SQLAlchemyError:
                db.session.rollback()
                flash('거래 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')

        elif trade.status == Trade.STATUS_ACCEPTED:
            # ACCEPTED 취소: 구매자 또는 판매자 모두 가능, 상품이 RESERVED 상태여야 함
            if product is None or product.seller_id != trade.seller_id or product.status != Product.STATUS_RESERVED:
                flash(_TRADE_AUTH_ERROR)
                return redirect(url_for('trades'))
            try:
                trade.status = Trade.STATUS_CANCELLED
                product.status = Product.STATUS_SELLING  # 상품 상태 복구
                db.session.commit()
                flash('거래가 취소되었습니다. 상품이 다시 판매 중 상태로 변경되었습니다.')
            except SQLAlchemyError:
                db.session.rollback()
                flash('거래 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')

        else:
            # REJECTED 또는 CANCELLED — 취소 불가
            flash(_TRADE_AUTH_ERROR)

        return redirect(url_for('trades'))

    # -------------------------------------------------------------------
    # 거래 완료 — POST /trade/<trade_id>/complete
    # -------------------------------------------------------------------

    @app.route('/trade/<trade_id>/complete', methods=['POST'])
    @trade_completion_action_limit
    @login_required
    def trade_complete(trade_id):
        current_user_id = session['user_id']
        validated_trade_id = _normalize_trade_id(trade_id)
        if validated_trade_id is None:
            flash('거래를 처리할 수 없습니다.')
            return redirect(url_for('trades'))

        trade = db.session.get(Trade, validated_trade_id)
        if trade is None or trade.buyer_id != current_user_id:
            flash('거래를 처리할 수 없습니다.')
            return redirect(url_for('trades'))

        if trade.status == Trade.STATUS_COMPLETED:
            flash('이미 완료된 거래입니다.')
            return redirect(url_for('trades'))

        if trade.status != Trade.STATUS_ACCEPTED:
            flash('거래를 처리할 수 없습니다.')
            return redirect(url_for('trades'))

        product = db.session.get(Product, trade.product_id)
        if product is None or product.seller_id != trade.seller_id or product.status != Product.STATUS_RESERVED:
            flash('거래를 처리할 수 없습니다.')
            return redirect(url_for('trades'))

        try:
            trade.status = Trade.STATUS_COMPLETED
            product.status = Product.STATUS_SOLD
            db.session.commit()
            flash('거래가 완료되었습니다.')
        except SQLAlchemyError:
            db.session.rollback()
            flash('거래 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')

        return redirect(url_for('trades'))

    # -------------------------------------------------------------------
    # 거래 리뷰 — GET/POST /trade/<trade_id>/review
    # -------------------------------------------------------------------

    @app.route('/trade/<trade_id>/review', methods=['GET', 'POST'])
    @review_action_limit
    @login_required
    def trade_review(trade_id):
        current_user_id = session['user_id']
        context = _review_context(trade_id, current_user_id)
        if context is None:
            flash('리뷰를 작성할 수 없습니다.')
            return redirect(url_for('trades'))

        if context['existing_review'] is not None:
            if request.method == 'POST':
                flash('이미 이 거래에 대한 리뷰를 작성했습니다.')
                return redirect(url_for('trades'))
            return render_template(
                'review_form.html',
                trade=context['trade'],
                product=context['product'],
                reviewee=context['reviewee'],
                already_reviewed=True,
            )

        if request.method == 'POST':
            rating, content, error = _validate_review_fields(request.form)
            if error:
                flash(error)
                return redirect(url_for('trade_review', trade_id=context['trade'].id))

            review = Review(
                trade_id=context['trade'].id,
                product_id=context['product'].id,
                reviewer_id=current_user_id,
                reviewee_id=context['reviewee'].id,
                rating=rating,
                content=content,
            )

            try:
                db.session.add(review)
                db.session.commit()
                flash('리뷰가 등록되었습니다.')
                return redirect(url_for('trades'))
            except IntegrityError:
                db.session.rollback()
                flash('리뷰를 등록할 수 없습니다.')
                return redirect(url_for('trades'))
            except SQLAlchemyError:
                db.session.rollback()
                flash('리뷰 등록 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')
                return redirect(url_for('trade_review', trade_id=context['trade'].id))

        return render_template(
            'review_form.html',
            trade=context['trade'],
            product=context['product'],
            reviewee=context['reviewee'],
            already_reviewed=False,
        )

    # -------------------------------------------------------------------
    # 신고
    # -------------------------------------------------------------------

    @app.route('/report', methods=['GET', 'POST'])
    @limiter.limit('5 per hour', methods=['POST'], key_func=_rate_limit_identity)
    @login_required
    def report():
        if request.method == 'POST':
            target_type = (request.form.get('target_type') or '').strip().upper()
            target_id = (request.form.get('target_id') or '').strip()
            reason = (request.form.get('reason') or '').strip()

            target = _resolve_report_target(target_type, target_id)
            if target is None:
                flash('신고 대상을 찾을 수 없거나 올바르지 않은 요청입니다.')
                return redirect(url_for('report'))

            if not reason:
                flash('신고 사유를 입력해 주세요.')
                return redirect(url_for('report'))
            if '\x00' in reason:
                flash('올바르지 않은 신고 사유입니다.')
                return redirect(url_for('report'))
            if len(reason) > 1000:
                flash('신고 사유는 1000자 이하여야 합니다.')
                return redirect(url_for('report'))

            current_user_id = session['user_id']

            # 본인 신고 방지
            if target_type == Report.TARGET_USER and target.id == current_user_id:
                flash('자기 자신을 신고할 수 없습니다.')
                return redirect(url_for('report'))
            if target_type == Report.TARGET_PRODUCT and target.seller_id == current_user_id:
                flash('자신의 상품을 신고할 수 없습니다.')
                return redirect(url_for('report'))

            # 중복 접수 방지
            try:
                existing_report = Report.query.filter_by(
                    reporter_id=current_user_id,
                    target_type=target_type,
                    target_id=target.id,
                    status=Report.STATUS_PENDING,
                ).first()

                if existing_report is not None:
                    flash('해당 대상에 대해 이미 처리 대기 중인 신고가 있습니다.')
                    return redirect(url_for('dashboard'))

                new_report = Report(
                    reporter_id=current_user_id,
                    target_type=target_type,
                    target_id=target.id,
                    reason=reason,
                    status=Report.STATUS_PENDING,
                )

                db.session.add(new_report)
                db.session.commit()
                flash('신고가 접수되었습니다.')

            except SQLAlchemyError:
                db.session.rollback()
                flash('신고 접수 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')

            return redirect(url_for('dashboard'))
        return render_template('report.html')

    # -------------------------------------------------------------------
    # 관리자 - 신고 목록
    # -------------------------------------------------------------------

    admin_required = _get_admin_required(app)

    # -------------------------------------------------------------------
    # 관리자 - 사용자 관리
    # -------------------------------------------------------------------

    @app.route('/admin/users')
    @admin_required
    def admin_users():
        users = User.query.order_by(User.username.asc(), User.id.asc()).all()
        return render_template('admin_users.html', users=users)

    # -------------------------------------------------------------------
    # 관리자 - 사용자 BAN
    # -------------------------------------------------------------------

    @app.route('/admin/user/<user_id>/ban', methods=['POST'])
    @admin_user_action_limit
    @admin_required
    def admin_user_ban(user_id):
        try:
            target_user_id = str(uuid.UUID(str(user_id)))
        except (ValueError, TypeError, AttributeError):
            flash('사용자 상태를 변경할 수 없습니다.')
            return redirect(url_for('admin_users'))

        target = db.session.get(User, target_user_id)
        if target is None:
            flash('사용자 상태를 변경할 수 없습니다.')
            return redirect(url_for('admin_users'))

        if target.id == session['user_id'] or target.role != User.ROLE_USER:
            flash('사용자 상태를 변경할 수 없습니다.')
            return redirect(url_for('admin_users'))

        try:
            if target.is_active:
                target.is_active = False
                db.session.commit()
                flash('사용자가 BAN 처리되었습니다.')
            else:
                flash('사용자가 이미 BAN 상태입니다.')
        except SQLAlchemyError:
            db.session.rollback()
            flash('사용자 상태 변경 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')

        return redirect(url_for('admin_users'))

    # -------------------------------------------------------------------
    # 관리자 - 사용자 BAN 해제
    # -------------------------------------------------------------------

    @app.route('/admin/user/<user_id>/unban', methods=['POST'])
    @admin_user_action_limit
    @admin_required
    def admin_user_unban(user_id):
        try:
            target_user_id = str(uuid.UUID(str(user_id)))
        except (ValueError, TypeError, AttributeError):
            flash('사용자 상태를 변경할 수 없습니다.')
            return redirect(url_for('admin_users'))

        target = db.session.get(User, target_user_id)
        if target is None:
            flash('사용자 상태를 변경할 수 없습니다.')
            return redirect(url_for('admin_users'))

        if target.role != User.ROLE_USER:
            flash('사용자 상태를 변경할 수 없습니다.')
            return redirect(url_for('admin_users'))

        try:
            if not target.is_active:
                target.is_active = True
                db.session.commit()
                flash('사용자 BAN이 해제되었습니다.')
            else:
                flash('사용자가 이미 활성 상태입니다.')
        except SQLAlchemyError:
            db.session.rollback()
            flash('사용자 상태 변경 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')

        return redirect(url_for('admin_users'))

    @app.route('/admin/reports')
    @admin_required
    def admin_reports():
        status_filter = request.args.get('status')
        q = Report.query

        valid_statuses = {Report.STATUS_PENDING, Report.STATUS_RESOLVED, Report.STATUS_DISMISSED}
        if status_filter in valid_statuses:
            q = q.filter(Report.status == status_filter)

        reports = q.order_by(Report.created_at.desc()).all()

        report_data = []
        for r in reports:
            target_summary = "삭제되었거나 존재하지 않는 대상"
            if r.target_type == Report.TARGET_USER:
                target_user = db.session.get(User, r.target_id)
                target_summary = target_user.username if target_user else "삭제되었거나 존재하지 않는 사용자"
            elif r.target_type == Report.TARGET_PRODUCT:
                target_product = db.session.get(Product, r.target_id)
                target_summary = target_product.title if target_product else "삭제되었거나 존재하지 않는 상품"

            report_data.append({
                'report': r,
                'target_summary': target_summary
            })

        return render_template('admin_reports.html', report_data=report_data, status_filter=status_filter, statuses=valid_statuses)

    # -------------------------------------------------------------------
    # 관리자 - 신고 상세
    # -------------------------------------------------------------------

    @app.route('/admin/report/<report_id>')
    @admin_required
    def admin_report_detail(report_id):
        try:
            report_uuid = str(uuid.UUID(str(report_id)))
        except (ValueError, TypeError, AttributeError):
            flash('올바르지 않은 신고 ID입니다.')
            return redirect(url_for('admin_reports'))

        report = db.session.get(Report, report_uuid)
        if report is None:
            flash('신고를 찾을 수 없습니다.')
            return redirect(url_for('admin_reports'))

        target_summary = "삭제되었거나 존재하지 않는 대상"
        if report.target_type == Report.TARGET_USER:
            target_user = db.session.get(User, report.target_id)
            target_summary = target_user.username if target_user else "삭제되었거나 존재하지 않는 사용자"
        elif report.target_type == Report.TARGET_PRODUCT:
            target_product = db.session.get(Product, report.target_id)
            target_summary = target_product.title if target_product else "삭제되었거나 존재하지 않는 상품"

        return render_template('admin_report_detail.html', report=report, target_summary=target_summary)

    # -------------------------------------------------------------------
    # 관리자 - 신고 처리 (해결)
    # -------------------------------------------------------------------

    @app.route('/admin/report/<report_id>/resolve', methods=['POST'])
    @admin_report_action_limit
    @admin_required
    def admin_report_resolve(report_id):
        try:
            report_uuid = str(uuid.UUID(str(report_id)))
        except (ValueError, TypeError, AttributeError):
            flash('올바르지 않은 신고 ID입니다.')
            return redirect(url_for('admin_reports'))

        report = db.session.get(Report, report_uuid)
        if report is None:
            flash('신고를 찾을 수 없습니다.')
            return redirect(url_for('admin_reports'))

        if report.status != Report.STATUS_PENDING:
            flash('이미 처리된 신고입니다.')
            return redirect(url_for('admin_report_detail', report_id=report_id))

        report.status = Report.STATUS_RESOLVED
        report.handled_by = session['user_id']

        try:
            db.session.commit()
            flash('신고가 해결 처리되었습니다.')
        except SQLAlchemyError:
            db.session.rollback()
            flash('신고 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')
        return redirect(url_for('admin_report_detail', report_id=report_id))

    # -------------------------------------------------------------------
    # 관리자 - 신고 처리 (거절)
    # -------------------------------------------------------------------

    @app.route('/admin/report/<report_id>/reject', methods=['POST'])
    @admin_report_action_limit
    @admin_required
    def admin_report_reject(report_id):
        try:
            report_uuid = str(uuid.UUID(str(report_id)))
        except (ValueError, TypeError, AttributeError):
            flash('올바르지 않은 신고 ID입니다.')
            return redirect(url_for('admin_reports'))

        report = db.session.get(Report, report_uuid)
        if report is None:
            flash('신고를 찾을 수 없습니다.')
            return redirect(url_for('admin_reports'))

        if report.status != Report.STATUS_PENDING:
            flash('이미 처리된 신고입니다.')
            return redirect(url_for('admin_report_detail', report_id=report_id))

        report.status = Report.STATUS_DISMISSED
        report.handled_by = session['user_id']

        try:
            db.session.commit()
            flash('신고가 기각 처리되었습니다.')
        except SQLAlchemyError:
            db.session.rollback()
            flash('신고 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')

        return redirect(url_for('admin_report_detail', report_id=report_id))

    # -------------------------------------------------------------------
    # 채팅 페이지 — GET /trade/<trade_id>/chat
    # -------------------------------------------------------------------

    @app.route('/trade/<trade_id>/chat')
    @login_required
    def trade_chat(trade_id):
        current_user_id = session['user_id']

        # trade_id를 UUID 문자열로 정규화 — 비UUID 값은 거부
        validated_trade_id = _normalize_trade_id(trade_id)
        if validated_trade_id is None:
            flash('채팅에 접근할 수 없거나 현재 이용할 수 없습니다.')
            return redirect(url_for('trades'))

        user, trade, product = _authorize_trade_chat(validated_trade_id, current_user_id)
        if user is None:
            flash('채팅에 접근할 수 없거나 현재 이용할 수 없습니다.')
            return redirect(url_for('trades'))

        # 상대방 결정 — 서버에서만 수행
        if current_user_id == trade.buyer_id:
            counterpart = trade.seller
        else:
            counterpart = trade.buyer

        return render_template(
            'trade_chat.html',
            trade=trade,
            product=product,
            current_user=user,
            counterpart=counterpart,
            trade_id=trade.id,
        )


# ---------------------------------------------------------------------------
# Socket.IO 이벤트 등록
# ---------------------------------------------------------------------------

# 중복 핸들러 등록 방지: 핸들러는 모듈 수준에서 한 번만 등록한다.
# create_app()이 여러 번 호출돼도 데코레이터가 중복 실행되지 않도록
# 등록 여부를 추적하는 플래그를 사용한다.
_socketio_events_registered = False


def _register_socketio_events():
    """보안 거래 채팅 Socket.IO 이벤트 핸들러를 등록한다.

    레거시 비인증 broadcast send_message 핸들러는 제거되었다.
    """
    global _socketio_events_registered
    if _socketio_events_registered:
        return
    _socketio_events_registered = True

    _CHAT_ERROR_MSG = '채팅에 접근할 수 없거나 현재 이용할 수 없습니다.'

    # ----------------------------------------------------------------
    # connect — 인증된 활성 사용자만 연결 허용
    # ----------------------------------------------------------------

    @socketio.on('connect')
    def handle_connect(auth=None):
        """세션에서 user_id를 읽어 사용자 인증 및 활성 상태를 확인한다.
        auth 파라미터는 수락하되 완전히 무시한다 — 클라이언트 제공 값은 신뢰하지 않는다."""
        from market.models import User

        user_id = session.get('user_id')
        if not user_id:
            return False  # 연결 거부

        try:
            user = db.session.get(User, user_id)
        except SQLAlchemyError:
            db.session.rollback()
            return False

        if user is None or not user.is_active:
            return False

        # 연결 수락 — 민감한 정보 emit 없음
        return True

    # ----------------------------------------------------------------
    # join_trade_chat — 거래 채팅방 입장
    # ----------------------------------------------------------------

    @socketio.on('join_trade_chat')
    def handle_join_trade_chat(data):
        """클라이언트가 제공한 trade_id를 정수로 변환한 뒤 권한을 검증하고
        서버에서 생성한 방 이름으로 입장한다. 클라이언트가 제공한 방 이름은
        절대 사용하지 않는다."""
        user_id = session.get('user_id')

        if not isinstance(data, dict):
            emit('chat_error', {'message': _CHAT_ERROR_MSG})
            return

        raw_trade_id = data.get('trade_id')
        trade_id = _normalize_trade_id(raw_trade_id)
        if trade_id is None:
            emit('chat_error', {'message': _CHAT_ERROR_MSG})
            return

        user, trade, product = _authorize_trade_chat(trade_id, user_id)
        if user is None:
            emit('chat_error', {'message': _CHAT_ERROR_MSG})
            return

        # 방 이름은 서버에서만 생성 — 클라이언트 입력 사용 금지
        room = f'trade:{trade.id}'
        join_room(room)

        # 최소 정보만 요청 소켓에만 전송 (broadcast 없음)
        emit('chat_joined', {
            'trade_id': trade.id,
            'message': '채팅방에 입장했습니다. 메시지는 새로고침 시 사라집니다.',
        })

    # ----------------------------------------------------------------
    # send_trade_message — 메시지 전송
    # ----------------------------------------------------------------

    @socketio.on('send_trade_message')
    def handle_send_trade_message(data):
        """메시지마다 권한을 재검증하고, 발신자 정보는 세션과 DB에서만 가져온다.
        메시지는 DB에 저장하지 않는다."""
        user_id = session.get('user_id')

        if not isinstance(data, dict):
            emit('chat_error', {'message': _CHAT_ERROR_MSG})
            return

        raw_trade_id = data.get('trade_id')
        trade_id = _normalize_trade_id(raw_trade_id)
        if trade_id is None:
            emit('chat_error', {'message': _CHAT_ERROR_MSG})
            return

        # 메시지 유효성 검사 — 클라이언트 값은 신뢰하지 않음
        message = data.get('message')
        if not isinstance(message, str):
            emit('chat_error', {'message': '올바르지 않은 메시지입니다.'})
            return

        message = message.strip()

        if not message:
            emit('chat_error', {'message': '메시지를 입력해 주세요.'})
            return

        if '\x00' in message:
            emit('chat_error', {'message': '올바르지 않은 메시지입니다.'})
            return

        if len(message) > 500:
            emit('chat_error', {'message': '메시지는 500자 이하여야 합니다.'})
            return

        # 모든 메시지마다 권한 재검증
        user, trade, product = _authorize_trade_chat(trade_id, user_id)
        if user is None:
            emit('chat_error', {'message': _CHAT_ERROR_MSG})
            return

        # 방 이름, 발신자 정보, 타임스탬프 모두 서버에서 생성
        room = f'trade:{trade.id}'
        sent_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

        # 해당 거래 방에만 전송 (broadcast=False, 전체 broadcast 없음)
        emit(
            'chat_message',
            {
                'trade_id': trade.id,
                'sender_id': user.id,
                'sender_username': user.username,
                'message': message,
                'sent_at': sent_at,
            },
            to=room,
        )

    # ----------------------------------------------------------------
    # leave_trade_chat — 채팅방 퇴장
    # ----------------------------------------------------------------

    @socketio.on('leave_trade_chat')
    def handle_leave_trade_chat(data):
        """클라이언트 제공 trade_id를 UUID로 정규화하고 권한을 재검증한 뒤
        서버에서 방 이름을 생성해 퇴장한다.
        DB를 수정하지 않는다. 다른 참가자에게 알리지 않는다."""
        user_id = session.get('user_id')

        if not isinstance(data, dict):
            return

        raw_trade_id = data.get('trade_id')
        trade_id = _normalize_trade_id(raw_trade_id)
        if trade_id is None:
            return

        # 권한 재검증 — 미인증 또는 상태 불일치 시 조용히 무시
        user, trade, product = _authorize_trade_chat(trade_id, user_id)
        if user is None:
            return

        # 방 이름은 검증된 trade.id를 사용 — 클라이언트 입력 사용 금지
        room = f'trade:{trade.id}'
        leave_room(room)
        # 다른 참가자에게 퇴장 사실을 알리지 않음
