
import pytest
from sqlalchemy import func, text

from market import create_app
from market.extensions import db, socketio
from market.models import (
    KeywordSubscription,
    Notification,
    PointLedger,
    PointWallet,
    Product,
    Report,
    Review,
    SupportTicket,
    Trade,
    TradePayment,
    User,
)
from market.points import MAX_POINT_BALANCE


TEST_PASSWORD = 'TestPassword123!'


@pytest.fixture
def csrf_app(tmp_path):
    app = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': True,
        'RATELIMIT_ENABLED': False,
        'SECRET_KEY': 'integration-csrf-secret',
        'PRODUCT_IMAGE_UPLOAD_DIR': str(tmp_path / 'uploads' / 'products'),
    })
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _login_session(client, user_id):
    with client.session_transaction() as sess:
        sess.clear()
        sess['user_id'] = user_id


def _create_user(username, role=User.ROLE_USER, is_active=True, balance=0):
    user = User(username=username, role=role, is_active=is_active)
    user.set_password(TEST_PASSWORD)
    db.session.add(user)
    db.session.flush()
    db.session.add(PointWallet(user_id=user.id, balance=balance))
    db.session.commit()
    return user.id


def _create_product(seller_id, title='통합 상품', description='설명', price=1000, status=Product.STATUS_SELLING):
    product = Product(
        seller_id=seller_id,
        title=title,
        description=description,
        price=price,
        trade_location='대구 수성구',
        status=status,
    )
    db.session.add(product)
    db.session.commit()
    return product.id


def _create_trade(buyer_id, seller_id, product_id, status=Trade.STATUS_PENDING):
    trade = Trade(product_id=product_id, buyer_id=buyer_id, seller_id=seller_id, status=status)
    product = db.session.get(Product, product_id)
    if status == Trade.STATUS_ACCEPTED:
        product.status = Product.STATUS_RESERVED
    elif status == Trade.STATUS_COMPLETED:
        product.status = Product.STATUS_SOLD
    db.session.add(trade)
    db.session.commit()
    return trade.id


def _create_held_payment(trade_id, amount=1000):
    trade = db.session.get(Trade, trade_id)
    payment = TradePayment(
        trade_id=trade.id,
        buyer_id=trade.buyer_id,
        seller_id=trade.seller_id,
        amount=amount,
        status=TradePayment.STATUS_HELD,
    )
    db.session.add(payment)
    db.session.flush()
    db.session.add(PointLedger(
        user_id=trade.buyer_id,
        payment_id=payment.id,
        trade_id=trade.id,
        entry_type=PointLedger.TYPE_ESCROW_DEBIT,
        amount=amount,
    ))
    db.session.commit()
    return payment.id


def _wallet_balance(user_id):
    return PointWallet.query.filter_by(user_id=user_id).one().balance


def _ledger_count(entry_type, payment_id=None):
    query = PointLedger.query.filter_by(entry_type=entry_type)
    if payment_id is not None:
        query = query.filter_by(payment_id=payment_id)
    return query.count()


def _assert_database_consistency():
    assert db.session.execute(text('PRAGMA foreign_key_check')).all() == []

    wallet_users = [
        row[0]
        for row in db.session.query(PointWallet.user_id)
        .group_by(PointWallet.user_id)
        .having(func.count(PointWallet.id) > 1)
        .all()
    ]
    assert wallet_users == []
    assert PointWallet.query.filter(
        db.or_(
            PointWallet.balance < 0,
            PointWallet.balance > MAX_POINT_BALANCE,
        )
    ).count() == 0

    duplicate_payments = (
        db.session.query(TradePayment.trade_id)
        .group_by(TradePayment.trade_id)
        .having(func.count(TradePayment.id) > 1)
        .all()
    )
    assert duplicate_payments == []

    duplicate_ledgers = (
        db.session.query(PointLedger.payment_id, PointLedger.user_id, PointLedger.entry_type)
        .filter(PointLedger.payment_id.isnot(None))
        .group_by(PointLedger.payment_id, PointLedger.user_id, PointLedger.entry_type)
        .having(func.count(PointLedger.id) > 1)
        .all()
    )
    assert duplicate_ledgers == []

    for payment in TradePayment.query.all():
        assert payment.trade is not None
        assert payment.buyer is not None
        assert payment.seller is not None
        assert payment.amount > 0
        assert payment.settled_at is None or payment.refunded_at is None

        ledger_types = {
            ledger.entry_type
            for ledger in PointLedger.query.filter_by(payment_id=payment.id).all()
        }
        assert not {
            PointLedger.TYPE_SETTLEMENT_CREDIT,
            PointLedger.TYPE_ESCROW_REFUND,
        }.issubset(ledger_types)

        if payment.status == TradePayment.STATUS_SETTLED:
            assert payment.settled_at is not None
            assert payment.refunded_at is None
            assert PointLedger.TYPE_SETTLEMENT_CREDIT in ledger_types
            assert PointLedger.TYPE_ESCROW_REFUND not in ledger_types
        elif payment.status == TradePayment.STATUS_REFUNDED:
            assert payment.refunded_at is not None
            assert payment.settled_at is None
            assert PointLedger.TYPE_ESCROW_REFUND in ledger_types
            assert PointLedger.TYPE_SETTLEMENT_CREDIT not in ledger_types
        elif payment.status == TradePayment.STATUS_HELD:
            assert payment.settled_at is None
            assert payment.refunded_at is None

    for ledger in PointLedger.query.all():
        assert ledger.user is not None
        assert ledger.amount > 0
        if ledger.entry_type == PointLedger.TYPE_ADMIN_GRANT:
            assert ledger.payment_id is None
            assert ledger.trade_id is None
        else:
            assert ledger.payment is not None
            assert ledger.trade is not None


def _assert_private_terms_absent(body):
    private_terms = [
        'password_hash',
        'SECRET_KEY',
        'SQLALCHEMY_DATABASE_URI',
        'sqlite://',
        'market.db',
        'session=',
        'Set-Cookie',
        'card',
        'bank',
        'account-number',
    ]
    for term in private_terms:
        assert term not in body


def test_successful_transaction_workflow_with_notifications_chat_payment_and_review(app):
    client = app.test_client()
    with app.app_context():
        admin_id = _create_user('integrationadmin', role=User.ROLE_ADMIN)
        seller_id = _create_user('integrationseller')
        buyer_id = _create_user('integrationbuyer')
        unrelated_id = _create_user('integrationother')

    _login_session(client, admin_id)
    assert client.post(f'/admin/user/{buyer_id}/points/grant', data={'amount': '3000'}).status_code == 302

    _login_session(client, buyer_id)
    assert client.post('/keywords', data={'keyword': '카메라'}).status_code == 302
    _login_session(client, seller_id)
    assert client.post('/keywords', data={'keyword': '카메라'}).status_code == 302

    unsafe_title = '<b>카메라</b>'
    _login_session(client, seller_id)
    response = client.post('/product/new', data={
        'title': unsafe_title,
        'description': '거래용 카메라 설명',
        'price': '1200',
        'trade_location': '대구 수성구',
        'seller_id': unrelated_id,
    })
    assert response.status_code == 302

    with app.app_context():
        product = Product.query.filter_by(title=unsafe_title).one()
        product_id = product.id
        assert product.seller_id == seller_id
        assert product.trade_location == '대구 수성구'
        assert Notification.query.filter_by(user_id=seller_id).count() == 0
        assert Notification.query.filter_by(user_id=buyer_id).count() == 1
        assert Notification.query.filter_by(user_id=unrelated_id).count() == 0
        notification = Notification.query.filter_by(user_id=buyer_id).one()
        assert notification.product_id == product_id

    _login_session(client, buyer_id)
    notification_page = client.get('/notifications')
    notification_body = notification_page.get_data(as_text=True)
    assert f'/product/{product_id}' in notification_body
    assert '&lt;b&gt;카메라&lt;/b&gt;' in notification_body
    assert unsafe_title not in notification_body

    assert client.post(f'/product/{product_id}/trade/request').status_code == 302
    with app.app_context():
        trade = Trade.query.filter_by(product_id=product_id, buyer_id=buyer_id).one()
        trade_id = trade.id

    _login_session(client, seller_id)
    assert client.post(f'/trade/{trade_id}/accept').status_code == 302
    with app.app_context():
        assert db.session.get(Product, product_id).status == Product.STATUS_RESERVED
        assert db.session.get(Trade, trade_id).status == Trade.STATUS_ACCEPTED
        before_pay_total = _wallet_balance(buyer_id) + _wallet_balance(seller_id)

    seller_trades = client.get('/trades')
    assert seller_trades.status_code == 200
    assert '&lt;b&gt;카메라&lt;/b&gt;' in seller_trades.get_data(as_text=True)
    seller_pay = client.post(f'/trade/{trade_id}/pay', data={'amount': '1'})
    assert seller_pay.status_code == 302
    with app.app_context():
        assert TradePayment.query.filter_by(trade_id=trade_id).count() == 0

    _login_session(client, unrelated_id)
    assert client.get(f'/trade/{trade_id}/chat').status_code == 302
    unrelated_trades = client.get('/trades')
    assert unsafe_title not in unrelated_trades.get_data(as_text=True)

    _login_session(client, buyer_id)
    buyer_trades = client.get('/trades')
    assert buyer_trades.status_code == 200
    assert '1200 포인트' in buyer_trades.get_data(as_text=True)

    buyer_http = app.test_client()
    seller_http = app.test_client()
    unrelated_http = app.test_client()
    _login_session(buyer_http, buyer_id)
    _login_session(seller_http, seller_id)
    _login_session(unrelated_http, unrelated_id)

    buyer_socket = socketio.test_client(app, flask_test_client=buyer_http)
    seller_socket = socketio.test_client(app, flask_test_client=seller_http)
    unrelated_socket = socketio.test_client(app, flask_test_client=unrelated_http)
    assert buyer_socket.is_connected()
    assert seller_socket.is_connected()
    assert unrelated_socket.is_connected()

    buyer_socket.emit('join_trade_chat', {'trade_id': trade_id})
    seller_socket.emit('join_trade_chat', {'trade_id': trade_id})
    unrelated_socket.emit('join_trade_chat', {'trade_id': trade_id})
    assert any(event['name'] == 'chat_joined' for event in buyer_socket.get_received())
    assert any(event['name'] == 'chat_joined' for event in seller_socket.get_received())
    assert any(event['name'] == 'chat_error' for event in unrelated_socket.get_received())

    unsafe_message = '<script>alert(1)</script>'
    buyer_socket.emit('send_trade_message', {'trade_id': trade_id, 'message': unsafe_message, 'sender_id': unrelated_id})
    seller_events = seller_socket.get_received()
    assert any(
        event['name'] == 'chat_message'
        and event['args'][0]['message'] == unsafe_message
        and event['args'][0]['sender_id'] == buyer_id
        for event in seller_events
    )
    chat_page = buyer_http.get(f'/trade/{trade_id}/chat').get_data(as_text=True)
    assert 'textContent' in chat_page
    assert unsafe_message not in chat_page

    pay_response = buyer_http.post(f'/trade/{trade_id}/pay', data={
        'amount': '1',
        'buyer_id': unrelated_id,
        'seller_id': unrelated_id,
        'product_id': unrelated_id,
        'status': TradePayment.STATUS_SETTLED,
    })
    repeat_pay_response = buyer_http.post(f'/trade/{trade_id}/pay', data={'amount': '1200'})
    assert pay_response.status_code == 302
    assert repeat_pay_response.status_code == 302
    with app.app_context():
        payment = TradePayment.query.filter_by(trade_id=trade_id).one()
        assert payment.amount == 1200
        assert payment.status == TradePayment.STATUS_HELD
        assert _wallet_balance(buyer_id) == 1800
        assert _wallet_balance(seller_id) == 0
        assert _ledger_count(PointLedger.TYPE_ESCROW_DEBIT, payment.id) == 1

    complete_response = buyer_http.post(f'/trade/{trade_id}/complete')
    repeat_complete_response = buyer_http.post(f'/trade/{trade_id}/complete')
    assert complete_response.status_code == 302
    assert repeat_complete_response.status_code == 302
    with app.app_context():
        payment = TradePayment.query.filter_by(trade_id=trade_id).one()
        assert payment.status == TradePayment.STATUS_SETTLED
        assert payment.settled_at is not None
        assert db.session.get(Trade, trade_id).status == Trade.STATUS_COMPLETED
        assert db.session.get(Product, product_id).status == Product.STATUS_SOLD
        assert _wallet_balance(buyer_id) == 1800
        assert _wallet_balance(seller_id) == 1200
        assert _wallet_balance(buyer_id) + _wallet_balance(seller_id) == before_pay_total
        assert _ledger_count(PointLedger.TYPE_SETTLEMENT_CREDIT, payment.id) == 1
        assert _ledger_count(PointLedger.TYPE_ESCROW_REFUND, payment.id) == 0
        _assert_database_consistency()

    review_response = buyer_http.post(f'/trade/{trade_id}/review', data={
        'rating': '5',
        'content': '<img src=x onerror=alert(1)>',
        'reviewer_id': unrelated_id,
        'reviewee_id': buyer_id,
        'product_id': unrelated_id,
    })
    duplicate_review_response = buyer_http.post(f'/trade/{trade_id}/review', data={
        'rating': '1',
        'content': 'duplicate',
    })
    assert review_response.status_code == 302
    assert duplicate_review_response.status_code == 302

    _login_session(client, unrelated_id)
    assert client.post(f'/trade/{trade_id}/review', data={'rating': '5', 'content': 'forged'}).status_code == 302

    with app.app_context():
        review = Review.query.one()
        assert review.trade_id == trade_id
        assert review.product_id == product_id
        assert review.reviewer_id == buyer_id
        assert review.reviewee_id == seller_id
        assert Review.query.count() == 1

    product_page = client.get(f'/product/{product_id}').get_data(as_text=True)
    assert '&lt;img src=x onerror=alert(1)&gt;' in product_page
    assert '<img src=x onerror=alert(1)>' not in product_page
    user_reviews = client.get(f'/user/{seller_id}/reviews').get_data(as_text=True)
    buyer_reviews = client.get(f'/user/{buyer_id}/reviews').get_data(as_text=True)
    assert '&lt;img src=x onerror=alert(1)&gt;' in user_reviews
    assert '&lt;img src=x onerror=alert(1)&gt;' not in buyer_reviews


def test_paid_trade_cancellation_refunds_once_and_blocks_completion_and_review(app):
    client = app.test_client()
    with app.app_context():
        admin_id = _create_user('refundadmin', role=User.ROLE_ADMIN)
        seller_id = _create_user('refundseller')
        buyer_id = _create_user('refundbuyer')
        product_id = _create_product(seller_id, title='환불 상품', price=700)

    _login_session(client, admin_id)
    assert client.post(f'/admin/user/{buyer_id}/points/grant', data={'amount': '1500'}).status_code == 302
    _login_session(client, buyer_id)
    assert client.post(f'/product/{product_id}/trade/request').status_code == 302
    with app.app_context():
        trade_id = Trade.query.filter_by(product_id=product_id, buyer_id=buyer_id).one().id
    _login_session(client, seller_id)
    assert client.post(f'/trade/{trade_id}/accept').status_code == 302
    _login_session(client, buyer_id)
    assert client.post(f'/trade/{trade_id}/pay').status_code == 302

    with app.app_context():
        payment = TradePayment.query.filter_by(trade_id=trade_id).one()
        payment_id = payment.id
        assert payment.status == TradePayment.STATUS_HELD
        assert _wallet_balance(buyer_id) == 800
        before_cancel_total = _wallet_balance(buyer_id) + _wallet_balance(seller_id)

    cancel_response = client.post(f'/trade/{trade_id}/cancel')
    repeat_cancel_response = client.post(f'/trade/{trade_id}/cancel')
    complete_response = client.post(f'/trade/{trade_id}/complete')
    review_response = client.post(f'/trade/{trade_id}/review', data={'rating': '5', 'content': 'cancelled'})
    assert cancel_response.status_code == 302
    assert repeat_cancel_response.status_code == 302
    assert complete_response.status_code == 302
    assert review_response.status_code == 302

    with app.app_context():
        payment = db.session.get(TradePayment, payment_id)
        assert payment.status == TradePayment.STATUS_REFUNDED
        assert payment.refunded_at is not None
        assert db.session.get(Trade, trade_id).status == Trade.STATUS_CANCELLED
        assert db.session.get(Product, product_id).status == Product.STATUS_SELLING
        assert _wallet_balance(buyer_id) == 1500
        assert _wallet_balance(seller_id) == 0
        assert _wallet_balance(buyer_id) + _wallet_balance(seller_id) == before_cancel_total + payment.amount
        assert _ledger_count(PointLedger.TYPE_ESCROW_REFUND, payment_id) == 1
        assert _ledger_count(PointLedger.TYPE_SETTLEMENT_CREDIT, payment_id) == 0
        assert Review.query.count() == 0
        _assert_database_consistency()


def test_reporting_and_support_remain_private_and_separate(app):
    client = app.test_client()
    with app.app_context():
        admin_id = _create_user('caseadmin', role=User.ROLE_ADMIN)
        reporter_id = _create_user('casereporter')
        target_id = _create_user('casetarget')
        other_id = _create_user('caseother')

    _login_session(client, reporter_id)
    assert client.post('/report', data={
        'target_type': Report.TARGET_USER,
        'target_id': target_id,
        'reason': '<script>report</script>',
    }).status_code == 302
    assert client.post('/support/new', data={
        'category': SupportTicket.CATEGORY_ACCOUNT,
        'title': '<b>지원 문의</b>',
        'content': '계정 문의 내용',
    }).status_code == 302

    with app.app_context():
        report = Report.query.one()
        ticket = SupportTicket.query.one()
        report_id = report.id
        ticket_id = ticket.id

    _login_session(client, other_id)
    assert client.get('/admin/reports').status_code == 302
    other_ticket_response = client.get(f'/support/{ticket_id}')
    assert other_ticket_response.status_code == 302
    assert '<b>지원 문의</b>' not in other_ticket_response.get_data(as_text=True)

    _login_session(client, reporter_id)
    ticket_page = client.get(f'/support/{ticket_id}')
    ticket_body = ticket_page.get_data(as_text=True)
    assert ticket_page.status_code == 200
    assert '&lt;b&gt;지원 문의&lt;/b&gt;' in ticket_body
    _assert_private_terms_absent(ticket_body)

    _login_session(client, admin_id)
    report_page = client.get(f'/admin/report/{report_id}')
    assert report_page.status_code == 200
    assert '&lt;script&gt;report&lt;/script&gt;' in report_page.get_data(as_text=True)
    assert client.post(f'/admin/report/{report_id}/resolve').status_code == 302
    support_admin_page = client.get(f'/admin/support/{ticket_id}')
    assert support_admin_page.status_code == 200
    assert '&lt;b&gt;지원 문의&lt;/b&gt;' in support_admin_page.get_data(as_text=True)
    assert client.post(f'/admin/support/{ticket_id}/update', data={
        'status': SupportTicket.STATUS_RESOLVED,
        'admin_response': '<b>처리 완료</b>',
        'user_id': other_id,
    }).status_code == 302
    updated_support = client.get(f'/admin/support/{ticket_id}').get_data(as_text=True)
    assert '&lt;b&gt;처리 완료&lt;/b&gt;' in updated_support
    assert '<b>처리 완료</b>' not in updated_support
    assert '<script>report</script>' not in updated_support

    reports_list = client.get('/admin/reports').get_data(as_text=True)
    support_list = client.get('/admin/support').get_data(as_text=True)
    assert '계정 문의 내용' not in reports_list
    assert '<script>report</script>' not in support_list
    with app.app_context():
        assert Report.query.count() == 1
        assert SupportTicket.query.count() == 1
        assert db.session.get(Report, report_id).status == Report.STATUS_RESOLVED
        assert db.session.get(SupportTicket, ticket_id).status == SupportTicket.STATUS_RESOLVED


def test_ban_inactive_user_denial_and_data_preservation(app):
    client = app.test_client()
    with app.app_context():
        admin_id = _create_user('banadmin', role=User.ROLE_ADMIN)
        banned_id = _create_user('banuser', balance=500)
        seller_id = _create_user('banseller')
        product_id = _create_product(banned_id, title='보존 상품')
        trade_id = _create_trade(banned_id, seller_id, _create_product(seller_id, title='보존 거래'), Trade.STATUS_COMPLETED)
        payment = TradePayment(
            trade_id=trade_id,
            buyer_id=banned_id,
            seller_id=seller_id,
            amount=100,
            status=TradePayment.STATUS_SETTLED,
            settled_at=func.now(),
        )
        db.session.add(payment)
        db.session.flush()
        db.session.add(PointLedger(
            user_id=banned_id,
            payment_id=payment.id,
            trade_id=trade_id,
            entry_type=PointLedger.TYPE_ESCROW_DEBIT,
            amount=100,
        ))
        db.session.add(PointLedger(
            user_id=seller_id,
            payment_id=payment.id,
            trade_id=trade_id,
            entry_type=PointLedger.TYPE_SETTLEMENT_CREDIT,
            amount=100,
        ))
        db.session.add(SupportTicket(user_id=banned_id, category=SupportTicket.CATEGORY_BUG, title='보존 문의', content='문의'))
        db.session.add(Report(reporter_id=banned_id, target_type=Report.TARGET_USER, target_id=seller_id, reason='신고'))
        db.session.add(Notification(user_id=banned_id, product_id=product_id, matched_keyword='보존'))
        db.session.add(Review(
            trade_id=trade_id,
            product_id=db.session.get(Trade, trade_id).product_id,
            reviewer_id=banned_id,
            reviewee_id=seller_id,
            rating=5,
            content='보존 리뷰',
        ))
        db.session.commit()
        before_counts = {
            model.__name__: model.query.count()
            for model in (User, PointWallet, PointLedger, Product, Trade, TradePayment, SupportTicket, Report, Notification, Review)
        }

    _login_session(client, admin_id)
    assert client.post(f'/admin/user/{banned_id}/ban').status_code == 302

    login_attempt = client.post('/login', data={'username': 'banuser', 'password': TEST_PASSWORD})
    assert login_attempt.status_code == 302
    assert login_attempt.headers['Location'].endswith('/login')

    denied_routes = [
        ('get', '/product/new'),
        ('post', '/product/new'),
        ('get', '/trades'),
        ('get', '/wallet'),
        ('get', '/support'),
        ('get', '/notifications'),
        ('get', f'/trade/{trade_id}/review'),
        ('get', f'/trade/{trade_id}/chat'),
    ]
    for method, path in denied_routes:
        _login_session(client, banned_id)
        response = getattr(client, method)(path, data={'title': 'x', 'description': 'x', 'price': '1'})
        assert response.status_code == 302
        assert '/login' in response.headers['Location']
        with client.session_transaction() as sess:
            assert 'user_id' not in sess

    _login_session(client, banned_id)
    assert client.post(f'/admin/user/{seller_id}/ban').status_code == 302
    with app.app_context():
        assert db.session.get(User, seller_id).is_active is True

    _login_session(client, admin_id)
    assert client.post(f'/admin/user/{banned_id}/unban').status_code == 302
    client.post('/logout')
    login_after_unban = client.post('/login', data={'username': 'banuser', 'password': TEST_PASSWORD})
    assert login_after_unban.status_code == 302
    assert login_after_unban.headers['Location'].endswith('/dashboard')

    with app.app_context():
        after_counts = {
            model.__name__: model.query.count()
            for model in (User, PointWallet, PointLedger, Product, Trade, TradePayment, SupportTicket, Report, Notification, Review)
        }
        assert before_counts == after_counts
        assert db.session.get(User, banned_id).is_active is True


def test_ownership_idor_matrix_for_cross_feature_routes(app):
    client = app.test_client()
    with app.app_context():
        admin_id = _create_user('idoradmin', role=User.ROLE_ADMIN)
        seller_id = _create_user('idorseller')
        buyer_id = _create_user('idorbuyer', balance=2000)
        other_id = _create_user('idorother')
        product_id = _create_product(seller_id, title='IDOR 상품', price=500, status=Product.STATUS_RESERVED)
        trade_id = _create_trade(buyer_id, seller_id, product_id, Trade.STATUS_ACCEPTED)
        other_product_id = _create_product(seller_id, title='삭제 방지 상품')
        ticket_id = SupportTicket(user_id=buyer_id, category=SupportTicket.CATEGORY_BUG, title='비공개 문의', content='비공개')
        db.session.add(ticket_id)
        subscription = KeywordSubscription(user_id=buyer_id, keyword='비밀', normalized_keyword='비밀')
        notification = Notification(user_id=buyer_id, product_id=product_id, matched_keyword='비밀')
        db.session.add_all([subscription, notification])
        db.session.commit()
        ticket_uuid = ticket_id.id
        subscription_id = subscription.id
        notification_id = notification.id

    _login_session(client, other_id)
    responses = [
        client.post(f'/product/{product_id}/edit', data={
            'title': '탈취',
            'description': '탈취',
            'price': '1',
            'trade_location': '서울',
            'status': Product.STATUS_SELLING,
        }),
        client.post(f'/product/{other_product_id}/delete'),
        client.get(f'/support/{ticket_uuid}'),
        client.get(f'/trade/{trade_id}/chat'),
        client.post(f'/trade/{trade_id}/pay'),
        client.post(f'/trade/{trade_id}/complete'),
        client.post(f'/trade/{trade_id}/cancel'),
        client.post(f'/trade/{trade_id}/review', data={'rating': '5', 'content': 'forged'}),
        client.post(f'/notifications/{notification_id}/read'),
        client.post(f'/keywords/{subscription_id}/delete'),
        client.get('/admin/reports'),
        client.get('/admin/support'),
        client.get('/admin/users'),
        client.post(f'/admin/user/{buyer_id}/ban'),
        client.post(f'/admin/user/{buyer_id}/points/grant', data={'amount': '100'}),
    ]
    assert all(response.status_code in {302, 403, 404} for response in responses)

    wallet_page = client.get('/wallet').get_data(as_text=True)
    assert '2000 포인트' not in wallet_page

    with app.app_context():
        assert db.session.get(Product, product_id).title == 'IDOR 상품'
        assert db.session.get(Product, other_product_id) is not None
        assert db.session.get(SupportTicket, ticket_uuid).user_id == buyer_id
        assert TradePayment.query.filter_by(trade_id=trade_id).count() == 0
        assert Review.query.count() == 0
        assert db.session.get(Notification, notification_id).is_read is False
        assert db.session.get(KeywordSubscription, subscription_id) is not None
        assert db.session.get(User, buyer_id).is_active is True
        assert _wallet_balance(buyer_id) == 2000
        assert PointLedger.query.count() == 0
        _assert_database_consistency()

    _login_session(client, admin_id)
    assert client.get('/admin/users').status_code == 200


def test_csrf_rejects_representative_state_changes(csrf_app):
    client = csrf_app.test_client()
    with csrf_app.app_context():
        admin_id = _create_user('csrfadmin', role=User.ROLE_ADMIN)
        seller_id = _create_user('csrfseller', balance=0)
        buyer_id = _create_user('csrfbuyer', balance=3000)
        inactive_target_id = _create_user('csrfbanned', is_active=False)
        product_delete_id = _create_product(seller_id, title='csrf delete')
        product_request_id = _create_product(seller_id, title='csrf request')
        pending_trade_id = _create_trade(buyer_id, seller_id, product_request_id, Trade.STATUS_PENDING)
        accepted_product_id = _create_product(seller_id, title='csrf accepted', status=Product.STATUS_RESERVED)
        accepted_trade_id = _create_trade(buyer_id, seller_id, accepted_product_id, Trade.STATUS_ACCEPTED)
        held_payment_id = _create_held_payment(accepted_trade_id, amount=100)
        review_product_id = _create_product(seller_id, title='csrf review', status=Product.STATUS_SOLD)
        completed_trade_id = _create_trade(buyer_id, seller_id, review_product_id, Trade.STATUS_COMPLETED)
        ticket = SupportTicket(user_id=buyer_id, category=SupportTicket.CATEGORY_BUG, title='csrf ticket', content='content')
        subscription = KeywordSubscription(user_id=buyer_id, keyword='csrf', normalized_keyword='csrf')
        db.session.add_all([ticket, subscription])
        db.session.commit()
        ticket_id = ticket.id
        subscription_id = subscription.id

    user_posts = [
        ('/logout', {}),
        ('/product/new', {'title': 'x', 'description': 'x', 'price': '1', 'trade_location': '서울'}),
        (f'/product/{product_delete_id}/delete', {}),
        (f'/product/{product_request_id}/trade/request', {}),
        (f'/trade/{pending_trade_id}/accept', {}),
        (f'/trade/{accepted_trade_id}/pay', {}),
        (f'/trade/{accepted_trade_id}/complete', {}),
        (f'/trade/{accepted_trade_id}/cancel', {}),
        (f'/trade/{completed_trade_id}/review', {'rating': '5', 'content': 'csrf'}),
        ('/keywords', {'keyword': 'csrf2'}),
        (f'/keywords/{subscription_id}/delete', {}),
        ('/support/new', {'category': SupportTicket.CATEGORY_BUG, 'title': 'csrf', 'content': 'csrf'}),
    ]
    _login_session(client, buyer_id)
    for path, data in user_posts:
        assert client.post(path, data=data).status_code == 400

    admin_posts = [
        (f'/admin/support/{ticket_id}/update', {'status': SupportTicket.STATUS_RESOLVED, 'admin_response': 'done'}),
        (f'/admin/user/{buyer_id}/points/grant', {'amount': '1'}),
        (f'/admin/user/{buyer_id}/ban', {}),
        (f'/admin/user/{inactive_target_id}/unban', {}),
    ]
    _login_session(client, admin_id)
    for path, data in admin_posts:
        assert client.post(path, data=data).status_code == 400

    assert client.post('/logout', data={'csrf_token': 'invalid'}).status_code == 400
    assert client.post(
        f'/admin/user/{buyer_id}/points/grant',
        data={'csrf_token': 'invalid', 'amount': '1'},
    ).status_code == 400

    with csrf_app.app_context():
        assert db.session.get(Product, product_delete_id) is not None
        assert db.session.get(Trade, accepted_trade_id).status == Trade.STATUS_ACCEPTED
        assert db.session.get(TradePayment, held_payment_id).status == TradePayment.STATUS_HELD
        assert Review.query.count() == 0
        assert db.session.get(KeywordSubscription, subscription_id) is not None
        assert db.session.get(SupportTicket, ticket_id).status == SupportTicket.STATUS_OPEN
        assert db.session.get(User, buyer_id).is_active is True


def test_security_headers_and_privacy_regression(app):
    client = app.test_client()
    with app.app_context():
        user_id = _create_user('privacyuser', balance=123)
        other_id = _create_user('privacyother', balance=9999)
        db.session.add(PointLedger(user_id=user_id, entry_type=PointLedger.TYPE_ADMIN_GRANT, amount=123))
        db.session.add(PointLedger(user_id=other_id, entry_type=PointLedger.TYPE_ADMIN_GRANT, amount=9999))
        db.session.commit()

    responses = [client.get('/login')]
    _login_session(client, user_id)
    responses.extend([client.get('/dashboard'), client.get('/wallet'), client.get('/notifications')])

    for response in responses:
        assert response.headers.get('X-Content-Type-Options') == 'nosniff'
        assert response.headers.get('X-Frame-Options') == 'DENY'
        assert response.headers.get('Referrer-Policy') == 'strict-origin-when-cross-origin'
        assert response.headers.get('Permissions-Policy') == 'camera=(), microphone=(), geolocation=()'
        _assert_private_terms_absent(response.get_data(as_text=True))

    wallet_body = responses[2].get_data(as_text=True)
    assert '123 포인트' in wallet_body
    assert '9999 포인트' not in wallet_body
    assert other_id not in wallet_body
