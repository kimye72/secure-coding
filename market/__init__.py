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


# ---------------------------------------------------------------------------
# 라우트 등록
# ---------------------------------------------------------------------------

def _register_routes(app):
    """모든 애플리케이션 라우트를 등록한다."""
    from market.models import User, Product, Trade, Report

    login_required = _get_login_required(app)

    # 허용된 상품 상태 집합
    _VALID_STATUSES = {Product.STATUS_SELLING, Product.STATUS_RESERVED, Product.STATUS_SOLD}

    # 편집/삭제 시 공통으로 사용하는 권한 없음 메시지
    _PRODUCT_AUTH_ERROR = '상품을 찾을 수 없거나 수정 권한이 없습니다.'

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
    # 대시보드 — 상품 목록 + 검색/필터
    # -------------------------------------------------------------------

    @app.route('/dashboard')
    @login_required
    def dashboard():
        user = db.session.get(User, session['user_id'])

        # ── 검색어 처리 ────────────────────────────────────────────────
        query_raw = (request.args.get('q') or '').strip()
        query = query_raw[:100]  # 최대 100자로 제한

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

        products = q.order_by(Product.created_at.desc()).all()

        return render_template(
            'dashboard.html',
            user=user,
            products=products,
            query=query,
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
    # 새 상품 등록 (강화된 버전)
    # -------------------------------------------------------------------

    @app.route('/product/new', methods=['GET', 'POST'])
    @login_required
    def new_product():
        if request.method == 'POST':
            # 공유 유효성 검사 헬퍼 사용 — seller_id는 절대로 폼에서 읽지 않음
            title, description, price, error = _validate_product_fields(request.form)
            if error:
                flash(error)
                return redirect(url_for('new_product'))

            new_prod = Product(
                title=title,
                description=description,
                price=price,
                seller_id=session['user_id'],          # 항상 세션에서 설정
                status=Product.STATUS_SELLING,          # 초기 상태는 항상 SELLING
            )
            try:
                db.session.add(new_prod)
                db.session.commit()
                flash('상품이 등록되었습니다.')
                return redirect(url_for('dashboard'))
            except SQLAlchemyError:
                db.session.rollback()
                flash('상품 등록 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')
                return redirect(url_for('new_product'))

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

        return render_template(
            'view_product.html',
            product=product,
            seller=seller,
            is_owner=is_owner,
            current_user_id=current_user_id,
            active_trade=active_trade,
            can_request_trade=can_request_trade,
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

            # ── 안전한 필드만 업데이트 (seller_id 절대 변경하지 않음) ──
            product.title = title
            product.description = description
            product.price = price
            product.status = status

            try:
                db.session.commit()
                flash('상품이 수정되었습니다.')
                return redirect(url_for('view_product', product_id=product_id))
            except SQLAlchemyError:
                db.session.rollback()
                flash('상품 수정 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')
                return redirect(url_for('edit_product', product_id=product_id))

        # GET: 기존 값으로 폼 사전 채움
        return render_template('edit_product.html', product=product)

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
            db.session.delete(product)
            db.session.commit()
            flash('상품이 삭제되었습니다.')
        except SQLAlchemyError:
            db.session.rollback()
            flash('상품 삭제 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.')

        return redirect(url_for('dashboard'))

    # -------------------------------------------------------------------
    # 거래 요청 생성 — POST /product/<product_id>/trade/request
    # -------------------------------------------------------------------

    @app.route('/product/<product_id>/trade/request', methods=['POST'])
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

        return render_template(
            'trades.html',
            outgoing_trades=outgoing_trades,
            incoming_trades=incoming_trades,
            current_user_id=current_user_id,
        )

    # -------------------------------------------------------------------
    # 거래 수락 — POST /trade/<trade_id>/accept
    # -------------------------------------------------------------------

    @app.route('/trade/<trade_id>/accept', methods=['POST'])
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
