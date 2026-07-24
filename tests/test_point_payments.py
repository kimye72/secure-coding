import re
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from market import create_app
from market.extensions import db
from market.models import PointLedger, PointWallet, Product, Trade, TradePayment, User
from market.points import MAX_ADMIN_POINT_GRANT, MAX_POINT_BALANCE
from scripts.add_point_payments import migrate_point_payments_schema, payment_tables_are_compatible, table_is_compatible


TEST_PASSWORD = 'TestPassword123!'


@pytest.fixture
def csrf_app():
    app = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': True,
        'RATELIMIT_ENABLED': False,
        'SECRET_KEY': 'csrf-point-secret',
    })
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def rate_app():
    app = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': False,
        'RATELIMIT_ENABLED': True,
        'RATELIMIT_STORAGE_URI': 'memory://',
        'SECRET_KEY': 'rate-point-secret',
    })
    with app.app_context():
        db.create_all()
        admin = _create_user('rateadmin', role=User.ROLE_ADMIN)
        buyer = _create_user('ratebuyer')
        yield app, admin, buyer
        db.session.remove()
        db.drop_all()


def _login_session(client, user_id):
    with client.session_transaction() as sess:
        sess['user_id'] = user_id


def _csrf_token(response):
    match = re.search(
        r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']',
        response.get_data(as_text=True),
    )
    assert match is not None
    return match.group(1)


def _create_user(username, role=User.ROLE_USER, is_active=True, with_wallet=True, balance=0):
    user = User(username=username, role=role, is_active=is_active)
    user.set_password(TEST_PASSWORD)
    db.session.add(user)
    db.session.flush()
    if with_wallet:
        db.session.add(PointWallet(user_id=user.id, balance=balance))
    db.session.commit()
    return user.id


def _create_product(seller_id, price=1000, status=Product.STATUS_SELLING):
    product = Product(
        seller_id=seller_id,
        title='Point Product',
        description='Point Desc',
        price=price,
        status=status,
    )
    db.session.add(product)
    db.session.commit()
    return product.id


def _create_trade(buyer_id, seller_id, product_id, status=Trade.STATUS_ACCEPTED):
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


def test_newly_registered_user_receives_zero_balance_wallet(client, app):
    response = client.post('/register', data={'username': 'walletuser', 'password': TEST_PASSWORD})

    assert response.status_code == 302
    with app.app_context():
        user = User.query.filter_by(username='walletuser').one()
        wallet = PointWallet.query.filter_by(user_id=user.id).one()
        assert wallet.balance == 0


def test_existing_user_without_wallet_gets_one_lazily(client, app):
    with app.app_context():
        user_id = _create_user('legacywallet', with_wallet=False)
    _login_session(client, user_id)

    response = client.get('/wallet')

    assert response.status_code == 200
    with app.app_context():
        wallets = PointWallet.query.filter_by(user_id=user_id).all()
        assert len(wallets) == 1
        assert wallets[0].balance == 0


def test_wallet_page_requires_login_and_shows_only_own_ledger(client, app):
    assert client.get('/wallet').status_code == 302
    with app.app_context():
        user_id = _create_user('walletowner', balance=100)
        other_id = _create_user('walletother', balance=200)
        db.session.add(PointLedger(user_id=user_id, entry_type=PointLedger.TYPE_ADMIN_GRANT, amount=100))
        db.session.add(PointLedger(user_id=other_id, entry_type=PointLedger.TYPE_ADMIN_GRANT, amount=200))
        db.session.commit()
    _login_session(client, user_id)

    response = client.get('/wallet')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert '100 포인트' in body
    assert '200 포인트' not in body
    assert 'password_hash' not in body
    assert 'session' not in body


def test_normal_user_and_inactive_admin_cannot_grant_points(client, app):
    with app.app_context():
        user_id = _create_user('grantnormal')
        target_id = _create_user('granttarget')
        inactive_admin_id = _create_user('inactiveadmin', role=User.ROLE_ADMIN, is_active=False)
    _login_session(client, user_id)
    normal_response = client.post(f'/admin/user/{target_id}/points/grant', data={'amount': '100'})
    _login_session(client, inactive_admin_id)
    inactive_response = client.post(f'/admin/user/{target_id}/points/grant', data={'amount': '100'})

    assert normal_response.status_code == 302
    assert inactive_response.status_code == 302
    with app.app_context():
        assert _wallet_balance(target_id) == 0
        assert PointLedger.query.count() == 0


def test_admin_can_grant_valid_test_points_and_ignores_forged_fields(client, app):
    with app.app_context():
        admin_id = _create_user('grantadmin', role=User.ROLE_ADMIN)
        target_id = _create_user('granttarget2')
        other_id = _create_user('grantother')
    _login_session(client, admin_id)

    response = client.post(f'/admin/user/{target_id}/points/grant', data={
        'amount': '500',
        'user_id': other_id,
        'wallet_id': other_id,
        'entry_type': PointLedger.TYPE_ESCROW_REFUND,
    })

    assert response.status_code == 302
    with app.app_context():
        assert _wallet_balance(target_id) == 500
        assert _wallet_balance(other_id) == 0
        ledger = PointLedger.query.one()
        assert ledger.user_id == target_id
        assert ledger.entry_type == PointLedger.TYPE_ADMIN_GRANT
        assert ledger.amount == 500


@pytest.mark.parametrize('amount', ['0', '-1', '1.5', 'abc', str(MAX_ADMIN_POINT_GRANT + 1)])
def test_invalid_admin_grants_are_rejected(client, app, amount):
    with app.app_context():
        admin_id = _create_user(f'admin{amount.replace("-", "n").replace(".", "d")}', role=User.ROLE_ADMIN)
        target_id = _create_user(f'target{amount.replace("-", "n").replace(".", "d")}')
    _login_session(client, admin_id)

    response = client.post(f'/admin/user/{target_id}/points/grant', data={'amount': amount})

    assert response.status_code == 302
    with app.app_context():
        assert _wallet_balance(target_id) == 0
        assert PointLedger.query.count() == 0


def test_admin_grant_overflow_is_rejected(client, app):
    with app.app_context():
        admin_id = _create_user('overflowadmin', role=User.ROLE_ADMIN)
        target_id = _create_user('overflowtarget', balance=MAX_POINT_BALANCE)
    _login_session(client, admin_id)

    response = client.post(f'/admin/user/{target_id}/points/grant', data={'amount': '1'})

    assert response.status_code == 302
    with app.app_context():
        assert _wallet_balance(target_id) == MAX_POINT_BALANCE
        assert PointLedger.query.count() == 0


def test_repeated_independent_grants_are_allowed(client, app):
    with app.app_context():
        admin_id = _create_user('repeatadmin', role=User.ROLE_ADMIN)
        target_id = _create_user('repeattarget')
    _login_session(client, admin_id)

    first = client.post(f'/admin/user/{target_id}/points/grant', data={'amount': '10'})
    second = client.post(f'/admin/user/{target_id}/points/grant', data={'amount': '15'})

    assert first.status_code == 302
    assert second.status_code == 302
    with app.app_context():
        assert _wallet_balance(target_id) == 25
        assert PointLedger.query.count() == 2


def test_csrf_protects_grant_and_payment(csrf_app):
    client = csrf_app.test_client()
    with csrf_app.app_context():
        admin_id = _create_user('csrfadmin', role=User.ROLE_ADMIN)
        buyer_id = _create_user('csrfbuyer', balance=1000)
        seller_id = _create_user('csrfseller')
        product_id = _create_product(seller_id)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
    _login_session(client, admin_id)
    grant_without = client.post(f'/admin/user/{buyer_id}/points/grant', data={'amount': '1'})
    token = _csrf_token(client.get('/admin/users'))
    grant_with = client.post(f'/admin/user/{buyer_id}/points/grant', data={'amount': '1', 'csrf_token': token})

    _login_session(client, buyer_id)
    payment_without = client.post(f'/trade/{trade_id}/pay')

    assert grant_without.status_code == 400
    assert grant_with.status_code == 302
    assert payment_without.status_code == 400


def test_grant_rate_limit_is_applied(rate_app):
    app, admin_id, target_id = rate_app
    client = app.test_client()
    _login_session(client, admin_id)

    responses = [
        client.post(f'/admin/user/{target_id}/points/grant', data={'amount': '1'})
        for _ in range(31)
    ]

    assert responses[-1].status_code == 429


@pytest.mark.parametrize('actor', ['seller', 'unrelated', 'admin'])
def test_only_buyer_can_pay_accepted_trade(client, app, actor):
    with app.app_context():
        buyer_id = _create_user(f'buyer{actor}', balance=1000)
        seller_id = _create_user(f'seller{actor}')
        unrelated_id = _create_user(f'unrelated{actor}')
        admin_id = _create_user(f'admin{actor}', role=User.ROLE_ADMIN)
        product_id = _create_product(seller_id)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
        actor_id = {'seller': seller_id, 'unrelated': unrelated_id, 'admin': admin_id}[actor]
    _login_session(client, actor_id)

    response = client.post(f'/trade/{trade_id}/pay', data={'amount': '1'})

    assert response.status_code == 302
    with app.app_context():
        assert TradePayment.query.count() == 0
        assert _wallet_balance(buyer_id) == 1000


@pytest.mark.parametrize('status', [
    Trade.STATUS_PENDING,
    Trade.STATUS_REJECTED,
    Trade.STATUS_CANCELLED,
    Trade.STATUS_COMPLETED,
])
def test_buyer_cannot_pay_nonaccepted_trade(client, app, status):
    with app.app_context():
        buyer_id = _create_user(f'buyer{status}', balance=1000)
        seller_id = _create_user(f'seller{status}')
        product_id = _create_product(seller_id)
        trade_id = _create_trade(buyer_id, seller_id, product_id, status=status)
    _login_session(client, buyer_id)

    response = client.post(f'/trade/{trade_id}/pay')

    assert response.status_code == 302
    with app.app_context():
        assert TradePayment.query.count() == 0
        assert _wallet_balance(buyer_id) == 1000


def test_buyer_can_pay_with_sufficient_points_and_forged_amounts_are_ignored(client, app):
    with app.app_context():
        buyer_id = _create_user('paybuyer', balance=1500)
        seller_id = _create_user('payseller')
        product_id = _create_product(seller_id, price=1000)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
    _login_session(client, buyer_id)

    response = client.post(f'/trade/{trade_id}/pay', data={
        'amount': '1',
        'buyer_id': seller_id,
        'seller_id': buyer_id,
        'product_id': 'forged',
        'status': TradePayment.STATUS_SETTLED,
    })

    assert response.status_code == 302
    with app.app_context():
        payment = TradePayment.query.one()
        assert payment.amount == 1000
        assert payment.buyer_id == buyer_id
        assert payment.seller_id == seller_id
        assert payment.status == TradePayment.STATUS_HELD
        assert _wallet_balance(buyer_id) == 500
        assert _wallet_balance(seller_id) == 0
        ledger = PointLedger.query.one()
        assert ledger.entry_type == PointLedger.TYPE_ESCROW_DEBIT
        assert ledger.amount == 1000


def test_insufficient_balance_creates_no_payment_or_ledger(client, app):
    with app.app_context():
        buyer_id = _create_user('poorbuyer', balance=999)
        seller_id = _create_user('poorseller')
        product_id = _create_product(seller_id, price=1000)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
    _login_session(client, buyer_id)

    response = client.post(f'/trade/{trade_id}/pay')

    assert response.status_code == 302
    with app.app_context():
        assert TradePayment.query.count() == 0
        assert PointLedger.query.count() == 0
        assert _wallet_balance(buyer_id) == 999


def test_repeated_payment_does_not_charge_twice(client, app):
    with app.app_context():
        buyer_id = _create_user('repeatpaybuyer', balance=3000)
        seller_id = _create_user('repeatpayseller')
        product_id = _create_product(seller_id, price=1000)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
    _login_session(client, buyer_id)

    first = client.post(f'/trade/{trade_id}/pay')
    second = client.post(f'/trade/{trade_id}/pay')

    assert first.status_code == 302
    assert second.status_code == 302
    with app.app_context():
        assert TradePayment.query.count() == 1
        assert PointLedger.query.count() == 1
        assert _wallet_balance(buyer_id) == 2000


def test_payment_database_failure_rolls_back_wallet_payment_and_ledger(client, app, monkeypatch):
    with app.app_context():
        buyer_id = _create_user('failpaybuyer', balance=1500)
        seller_id = _create_user('failpayseller')
        product_id = _create_product(seller_id, price=1000)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
    _login_session(client, buyer_id)

    def fail_commit():
        raise SQLAlchemyError()

    with monkeypatch.context() as patch:
        patch.setattr(db.session, 'commit', fail_commit)
        response = client.post(f'/trade/{trade_id}/pay')

    assert response.status_code == 302
    with app.app_context():
        assert _wallet_balance(buyer_id) == 1500
        assert TradePayment.query.count() == 0
        assert PointLedger.query.count() == 0


def test_payment_uses_post_and_rate_limit_is_applied(rate_app):
    app, _admin_id, buyer_id = rate_app
    client = app.test_client()
    with app.app_context():
        seller_id = _create_user('ratepayseller')
        product_id = _create_product(seller_id)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
        PointWallet.query.filter_by(user_id=buyer_id).update({PointWallet.balance: 100000})
        db.session.commit()
    _login_session(client, buyer_id)

    assert client.get(f'/trade/{trade_id}/pay').status_code == 405
    responses = [client.post(f'/trade/{trade_id}/pay') for _ in range(21)]
    assert responses[-1].status_code == 429


def test_accepted_unpaid_trade_cannot_complete(client, app):
    with app.app_context():
        buyer_id = _create_user('unpaidbuyer')
        seller_id = _create_user('unpaidseller')
        product_id = _create_product(seller_id)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
    _login_session(client, buyer_id)

    response = client.post(f'/trade/{trade_id}/complete')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Trade, trade_id).status == Trade.STATUS_ACCEPTED


def test_paid_trade_completion_settles_once_and_marks_sold(client, app):
    with app.app_context():
        buyer_id = _create_user('settlebuyer')
        seller_id = _create_user('settleseller')
        product_id = _create_product(seller_id, price=1000)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
        payment_id = _create_held_payment(trade_id, amount=1000)
    _login_session(client, buyer_id)

    first = client.post(f'/trade/{trade_id}/complete')
    second = client.post(f'/trade/{trade_id}/complete')

    assert first.status_code == 302
    assert second.status_code == 302
    with app.app_context():
        payment = db.session.get(TradePayment, payment_id)
        assert payment.status == TradePayment.STATUS_SETTLED
        assert payment.settled_at is not None
        assert db.session.get(Trade, trade_id).status == Trade.STATUS_COMPLETED
        assert db.session.get(Product, product_id).status == Product.STATUS_SOLD
        assert _wallet_balance(seller_id) == 1000
        assert PointLedger.query.filter_by(entry_type=PointLedger.TYPE_SETTLEMENT_CREDIT).count() == 1


def test_seller_cannot_complete_paid_trade(client, app):
    with app.app_context():
        buyer_id = _create_user('sellercompletebuyer')
        seller_id = _create_user('sellercompleteseller')
        product_id = _create_product(seller_id)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
        _create_held_payment(trade_id)
    _login_session(client, seller_id)

    response = client.post(f'/trade/{trade_id}/complete')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Trade, trade_id).status == Trade.STATUS_ACCEPTED


def test_seller_overflow_prevents_settlement_and_rolls_back(client, app):
    with app.app_context():
        buyer_id = _create_user('overflowsettlebuyer')
        seller_id = _create_user('overflowsettleseller', balance=MAX_POINT_BALANCE)
        product_id = _create_product(seller_id, price=1)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
        payment_id = _create_held_payment(trade_id, amount=1)
    _login_session(client, buyer_id)

    response = client.post(f'/trade/{trade_id}/complete')

    assert response.status_code == 302
    with app.app_context():
        assert _wallet_balance(seller_id) == MAX_POINT_BALANCE
        assert db.session.get(TradePayment, payment_id).status == TradePayment.STATUS_HELD
        assert db.session.get(Trade, trade_id).status == Trade.STATUS_ACCEPTED
        assert PointLedger.query.filter_by(entry_type=PointLedger.TYPE_SETTLEMENT_CREDIT).count() == 0


def test_valid_cancellation_of_held_payment_refunds_once(client, app):
    with app.app_context():
        buyer_id = _create_user('refundbuyer', balance=0)
        seller_id = _create_user('refundseller')
        product_id = _create_product(seller_id, price=1000)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
        payment_id = _create_held_payment(trade_id, amount=1000)
    _login_session(client, buyer_id)

    first = client.post(f'/trade/{trade_id}/cancel')
    second = client.post(f'/trade/{trade_id}/cancel')

    assert first.status_code == 302
    assert second.status_code == 302
    with app.app_context():
        payment = db.session.get(TradePayment, payment_id)
        assert payment.status == TradePayment.STATUS_REFUNDED
        assert payment.refunded_at is not None
        assert _wallet_balance(buyer_id) == 1000
        assert db.session.get(Trade, trade_id).status == Trade.STATUS_CANCELLED
        assert db.session.get(Product, product_id).status == Product.STATUS_SELLING
        assert PointLedger.query.filter_by(entry_type=PointLedger.TYPE_ESCROW_REFUND).count() == 1


def test_payment_after_cancellation_creates_no_debit_payment_or_ledger(client, app):
    with app.app_context():
        buyer_id = _create_user('cancelwinbuyer', balance=1000)
        seller_id = _create_user('cancelwinseller')
        product_id = _create_product(seller_id, price=1000)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
    _login_session(client, seller_id)
    cancel_response = client.post(f'/trade/{trade_id}/cancel')
    _login_session(client, buyer_id)
    pay_response = client.post(f'/trade/{trade_id}/pay')

    assert cancel_response.status_code == 302
    assert pay_response.status_code == 302
    with app.app_context():
        assert db.session.get(Trade, trade_id).status == Trade.STATUS_CANCELLED
        assert TradePayment.query.count() == 0
        assert PointLedger.query.count() == 0
        assert _wallet_balance(buyer_id) == 1000


def test_file_backed_sqlite_payment_and_cancellation_winner_state_is_consistent(tmp_path):
    file_app = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f"sqlite:///{tmp_path / 'interleaving.sqlite'}",
        'WTF_CSRF_ENABLED': False,
        'RATELIMIT_ENABLED': False,
        'SECRET_KEY': 'file-point-secret',
    })
    buyer_client = file_app.test_client()
    seller_client = file_app.test_client()
    with file_app.app_context():
        db.create_all()
        buyer_id = _create_user('filebuyer', balance=1000)
        seller_id = _create_user('fileseller')
        product_id = _create_product(seller_id, price=1000)
        trade_id = _create_trade(buyer_id, seller_id, product_id)

    _login_session(seller_client, seller_id)
    cancel_response = seller_client.post(f'/trade/{trade_id}/cancel')
    _login_session(buyer_client, buyer_id)
    pay_response = buyer_client.post(f'/trade/{trade_id}/pay')

    assert cancel_response.status_code == 302
    assert pay_response.status_code == 302
    with file_app.app_context():
        assert db.session.get(Trade, trade_id).status == Trade.STATUS_CANCELLED
        assert TradePayment.query.count() == 0
        assert PointLedger.query.count() == 0
        assert _wallet_balance(buyer_id) == 1000
        db.session.remove()
        db.drop_all()


def test_payment_then_cancellation_refunds_exactly_once_and_conserves_points(client, app):
    with app.app_context():
        buyer_id = _create_user('paycancelbuyer', balance=1000)
        seller_id = _create_user('paycancelseller')
        product_id = _create_product(seller_id, price=1000)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
    _login_session(client, buyer_id)
    pay_response = client.post(f'/trade/{trade_id}/pay')
    cancel_response = client.post(f'/trade/{trade_id}/cancel')
    repeated_cancel = client.post(f'/trade/{trade_id}/cancel')

    assert pay_response.status_code == 302
    assert cancel_response.status_code == 302
    assert repeated_cancel.status_code == 302
    with app.app_context():
        payment = TradePayment.query.filter_by(trade_id=trade_id).one()
        assert payment.status == TradePayment.STATUS_REFUNDED
        assert _wallet_balance(buyer_id) == 1000
        assert _wallet_balance(seller_id) == 0
        assert PointLedger.query.filter_by(payment_id=payment.id, entry_type=PointLedger.TYPE_ESCROW_DEBIT).count() == 1
        assert PointLedger.query.filter_by(payment_id=payment.id, entry_type=PointLedger.TYPE_ESCROW_REFUND).count() == 1
        assert PointLedger.query.filter_by(payment_id=payment.id, entry_type=PointLedger.TYPE_SETTLEMENT_CREDIT).count() == 0


def test_completion_then_cancellation_cannot_refund_or_double_credit(client, app):
    with app.app_context():
        buyer_id = _create_user('completecancelbuyer', balance=1000)
        seller_id = _create_user('completecancelseller')
        product_id = _create_product(seller_id, price=1000)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
    _login_session(client, buyer_id)
    pay_response = client.post(f'/trade/{trade_id}/pay')
    complete_response = client.post(f'/trade/{trade_id}/complete')
    cancel_response = client.post(f'/trade/{trade_id}/cancel')

    assert pay_response.status_code == 302
    assert complete_response.status_code == 302
    assert cancel_response.status_code == 302
    with app.app_context():
        payment = TradePayment.query.filter_by(trade_id=trade_id).one()
        assert payment.status == TradePayment.STATUS_SETTLED
        assert db.session.get(Trade, trade_id).status == Trade.STATUS_COMPLETED
        assert _wallet_balance(buyer_id) == 0
        assert _wallet_balance(seller_id) == 1000
        assert PointLedger.query.filter_by(payment_id=payment.id, entry_type=PointLedger.TYPE_ESCROW_REFUND).count() == 0
        assert PointLedger.query.filter_by(payment_id=payment.id, entry_type=PointLedger.TYPE_SETTLEMENT_CREDIT).count() == 1


def test_cancellation_then_completion_cannot_settle_refunded_payment(client, app):
    with app.app_context():
        buyer_id = _create_user('cancelcompletebuyer', balance=1000)
        seller_id = _create_user('cancelcompleteseller')
        product_id = _create_product(seller_id, price=1000)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
    _login_session(client, buyer_id)
    pay_response = client.post(f'/trade/{trade_id}/pay')
    cancel_response = client.post(f'/trade/{trade_id}/cancel')
    complete_response = client.post(f'/trade/{trade_id}/complete')

    assert pay_response.status_code == 302
    assert cancel_response.status_code == 302
    assert complete_response.status_code == 302
    with app.app_context():
        payment = TradePayment.query.filter_by(trade_id=trade_id).one()
        assert payment.status == TradePayment.STATUS_REFUNDED
        assert db.session.get(Trade, trade_id).status == Trade.STATUS_CANCELLED
        assert _wallet_balance(buyer_id) == 1000
        assert _wallet_balance(seller_id) == 0
        assert PointLedger.query.filter_by(payment_id=payment.id, entry_type=PointLedger.TYPE_ESCROW_REFUND).count() == 1
        assert PointLedger.query.filter_by(payment_id=payment.id, entry_type=PointLedger.TYPE_SETTLEMENT_CREDIT).count() == 0


def test_stale_preloaded_payment_state_does_not_enable_second_terminal_route(client, app):
    with app.app_context():
        buyer_id = _create_user('stalebuyer', balance=1000)
        seller_id = _create_user('staleseller')
        product_id = _create_product(seller_id, price=1000)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
    _login_session(client, buyer_id)
    client.post(f'/trade/{trade_id}/pay')
    with app.app_context():
        stale_payment = TradePayment.query.filter_by(trade_id=trade_id).one()
        assert stale_payment.status == TradePayment.STATUS_HELD
    complete_response = client.post(f'/trade/{trade_id}/complete')
    cancel_response = client.post(f'/trade/{trade_id}/cancel')

    assert complete_response.status_code == 302
    assert cancel_response.status_code == 302
    with app.app_context():
        payment = TradePayment.query.filter_by(trade_id=trade_id).one()
        assert payment.status == TradePayment.STATUS_SETTLED
        assert PointLedger.query.filter_by(payment_id=payment.id, entry_type=PointLedger.TYPE_ESCROW_REFUND).count() == 0
        assert PointLedger.query.filter_by(payment_id=payment.id, entry_type=PointLedger.TYPE_SETTLEMENT_CREDIT).count() == 1


def test_settled_payment_cannot_refund_and_unrelated_user_cannot_cancel(client, app):
    with app.app_context():
        buyer_id = _create_user('norefundbuyer')
        seller_id = _create_user('norefundseller', balance=1000)
        unrelated_id = _create_user('norefundother')
        product_id = _create_product(seller_id, status=Product.STATUS_SOLD)
        trade_id = _create_trade(buyer_id, seller_id, product_id, status=Trade.STATUS_COMPLETED)
        payment = TradePayment(
            trade_id=trade_id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            amount=1000,
            status=TradePayment.STATUS_SETTLED,
            settled_at=datetime.utcnow(),
        )
        db.session.add(payment)
        db.session.commit()
        payment_id = payment.id
    _login_session(client, unrelated_id)
    unrelated_response = client.post(f'/trade/{trade_id}/cancel')
    _login_session(client, buyer_id)
    buyer_response = client.post(f'/trade/{trade_id}/cancel')

    assert unrelated_response.status_code == 302
    assert buyer_response.status_code == 302
    with app.app_context():
        assert _wallet_balance(buyer_id) == 0
        assert db.session.get(TradePayment, payment_id).status == TradePayment.STATUS_SETTLED
        assert db.session.get(Trade, trade_id).status == Trade.STATUS_COMPLETED


def test_trade_page_payment_display_does_not_show_buyer_balance_to_seller(client, app):
    with app.app_context():
        buyer_id = _create_user('displaybuyer', balance=7777)
        seller_id = _create_user('displayseller')
        product_id = _create_product(seller_id)
        trade_id = _create_trade(buyer_id, seller_id, product_id)
        _create_held_payment(trade_id)
    _login_session(client, seller_id)

    response = client.get('/trades')
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert '결제 상태: HELD' in body
    assert '7777' not in body
    assert 'password_hash' not in body


def _migration_base_tables(engine):
    User.__table__.create(bind=engine)
    Product.__table__.create(bind=engine)
    Trade.__table__.create(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE unrelated_table (id INTEGER PRIMARY KEY, note TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO unrelated_table (id, note) VALUES (1, 'preserve')"))
        connection.execute(text(
            "INSERT INTO user (id, username, password_hash, bio, role, is_active, created_at) "
            "VALUES ('user1', 'user1', 'hash', NULL, 'USER', 1, '2026-01-01 00:00:00')"
        ))
        connection.execute(text(
            "INSERT INTO user (id, username, password_hash, bio, role, is_active, created_at) "
            "VALUES ('user2', 'user2', 'hash', NULL, 'USER', 1, '2026-01-01 00:00:00')"
        ))
        connection.execute(text(
            "INSERT INTO product (id, seller_id, title, description, price, trade_location, status, created_at, updated_at) "
            "VALUES ('product1', 'user1', 'Migration Product', 'Desc', 1000, NULL, 'RESERVED', "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        ))
        connection.execute(text(
            "INSERT INTO trade (id, product_id, buyer_id, seller_id, status, created_at, updated_at) "
            "VALUES ('trade1', 'product1', 'user2', 'user1', 'ACCEPTED', "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        ))


def _unrelated_rows(engine):
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text('SELECT id, note FROM unrelated_table')).all()]


def _table_sql(engine, table_name):
    with engine.connect() as connection:
        return connection.execute(text(
            'SELECT sql FROM sqlite_master WHERE type = "table" AND name = :table_name'
        ), {'table_name': table_name}).scalar_one()


def _table_rows(engine, table_name):
    with engine.connect() as connection:
        rows = connection.execute(text(f'SELECT * FROM {table_name} ORDER BY id')).all()
    return [tuple(row) for row in rows]


def _create_legacy_point_wallet_table(engine):
    with engine.begin() as connection:
        connection.execute(text(
            'CREATE TABLE point_wallet ('
            'id VARCHAR(36) NOT NULL, user_id VARCHAR(36) NOT NULL, balance INTEGER NOT NULL, '
            'created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, PRIMARY KEY (id), '
            'FOREIGN KEY(user_id) REFERENCES "user" (id), CHECK (balance >= 0))'
        ))
        connection.execute(text('CREATE UNIQUE INDEX ix_point_wallet_user_id ON point_wallet (user_id)'))
        connection.execute(text(
            "INSERT INTO point_wallet (id, user_id, balance, created_at, updated_at) "
            "VALUES ('wallet1', 'user1', 5, '2026-01-01', '2026-01-01')"
        ))


def _create_point_wallet_table_with_extra_constraints(
    engine,
    *,
    id_type='VARCHAR(36)',
    user_id_type='VARCHAR(36)',
    balance_type='INTEGER',
    extra_unique='',
    extra_fk='',
    extra_check='',
    expected_fk='FOREIGN KEY(user_id) REFERENCES "user" (id),',
    created_at_type='DATETIME',
    updated_at_type='DATETIME',
    create_required_index=True,
):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE wallet_parent (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO wallet_parent (id) VALUES ('user1')"))
        connection.execute(text(
            'CREATE TABLE point_wallet ('
            f'id {id_type} NOT NULL, user_id {user_id_type} NOT NULL, balance {balance_type} NOT NULL, '
            f'created_at {created_at_type} NOT NULL, updated_at {updated_at_type} NOT NULL, PRIMARY KEY (id), '
            f'{expected_fk} {extra_fk} {extra_unique} '
            f'CHECK (balance >= 0 AND balance <= {MAX_POINT_BALANCE}) {extra_check})'
        ))
        if create_required_index:
            connection.execute(text('CREATE UNIQUE INDEX ix_point_wallet_user_id ON point_wallet (user_id)'))
        connection.execute(text(
            "INSERT INTO point_wallet (id, user_id, balance, created_at, updated_at) "
            "VALUES ('wallet1', 'user1', 5, '2026-01-01', '2026-01-01')"
        ))


def _create_trade_payment_table_with_checks(
    engine,
    extra_checks,
    *,
    id_type='VARCHAR(36)',
    trade_id_type='VARCHAR(36)',
    buyer_id_type='VARCHAR(36)',
    seller_id_type='VARCHAR(36)',
    amount_type='INTEGER',
    status_type='VARCHAR(20)',
    created_at_type='DATETIME',
    updated_at_type='DATETIME',
    settled_at_type='DATETIME',
    refunded_at_type='DATETIME',
    trade_index_sql='CREATE UNIQUE INDEX ix_trade_payment_trade_id ON trade_payment (trade_id)',
    buyer_index_sql='CREATE INDEX ix_trade_payment_buyer_id ON trade_payment (buyer_id)',
):
    check_sql = ', '.join(f'CHECK ({check})' for check in extra_checks)
    with engine.begin() as connection:
        connection.execute(text(
            'CREATE TABLE trade_payment ('
            f'id {id_type} NOT NULL, trade_id {trade_id_type} NOT NULL, buyer_id {buyer_id_type} NOT NULL, '
            f'seller_id {seller_id_type} NOT NULL, amount {amount_type} NOT NULL, status {status_type} NOT NULL, '
            f'created_at {created_at_type} NOT NULL, updated_at {updated_at_type} NOT NULL, '
            f'settled_at {settled_at_type} NULL, refunded_at {refunded_at_type} NULL, PRIMARY KEY (id), {check_sql}, '
            'FOREIGN KEY(trade_id) REFERENCES trade (id), '
            'FOREIGN KEY(buyer_id) REFERENCES "user" (id), '
            'FOREIGN KEY(seller_id) REFERENCES "user" (id))'
        ))
        connection.execute(text(trade_index_sql))
        connection.execute(text(buyer_index_sql))
        connection.execute(text('CREATE INDEX ix_trade_payment_seller_id ON trade_payment (seller_id)'))
        connection.execute(text('CREATE INDEX ix_trade_payment_status ON trade_payment (status)'))


def _create_trade_payment_table_with_extra_unique(engine):
    with engine.begin() as connection:
        connection.execute(text(
            'CREATE TABLE trade_payment ('
            'id VARCHAR(36) NOT NULL, trade_id VARCHAR(36) NOT NULL, buyer_id VARCHAR(36) NOT NULL, '
            'seller_id VARCHAR(36) NOT NULL, amount INTEGER NOT NULL, status VARCHAR(20) NOT NULL, '
            'created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, settled_at DATETIME NULL, '
            'refunded_at DATETIME NULL, PRIMARY KEY (id), UNIQUE (status), '
            f'CHECK (amount >= 1 AND amount <= {MAX_POINT_BALANCE}), '
            'CHECK (buyer_id != seller_id), '
            "CHECK (status IN ('HELD', 'SETTLED', 'REFUNDED')), "
            "CHECK ((status = 'HELD' AND settled_at IS NULL AND refunded_at IS NULL) "
            "OR (status = 'SETTLED' AND settled_at IS NOT NULL AND refunded_at IS NULL) "
            "OR (status = 'REFUNDED' AND refunded_at IS NOT NULL AND settled_at IS NULL)), "
            'FOREIGN KEY(trade_id) REFERENCES trade (id), '
            'FOREIGN KEY(buyer_id) REFERENCES "user" (id), '
            'FOREIGN KEY(seller_id) REFERENCES "user" (id))'
        ))
        connection.execute(text('CREATE UNIQUE INDEX ix_trade_payment_trade_id ON trade_payment (trade_id)'))
        connection.execute(text('CREATE INDEX ix_trade_payment_buyer_id ON trade_payment (buyer_id)'))
        connection.execute(text('CREATE INDEX ix_trade_payment_seller_id ON trade_payment (seller_id)'))
        connection.execute(text('CREATE INDEX ix_trade_payment_status ON trade_payment (status)'))
        connection.execute(text(
            "INSERT INTO trade_payment "
            "(id, trade_id, buyer_id, seller_id, amount, status, created_at, updated_at, settled_at, refunded_at) "
            "VALUES ('payment1', 'trade1', 'user2', 'user1', 1000, 'HELD', "
            "'2026-01-01', '2026-01-01', NULL, NULL)"
        ))


def _create_point_ledger_table_with_checks(
    engine,
    extra_checks,
    *,
    id_type='VARCHAR(36)',
    user_id_type='VARCHAR(36)',
    payment_id_type='VARCHAR(36)',
    trade_id_type='VARCHAR(36)',
    entry_type_type='VARCHAR(30)',
    amount_type='INTEGER',
    created_at_type='DATETIME',
):
    check_sql = ', '.join(f'CHECK ({check})' for check in extra_checks)
    with engine.begin() as connection:
        connection.execute(text(
            'CREATE TABLE point_ledger ('
            f'id {id_type} NOT NULL, user_id {user_id_type} NOT NULL, payment_id {payment_id_type} NULL, '
            f'trade_id {trade_id_type} NULL, entry_type {entry_type_type} NOT NULL, amount {amount_type} NOT NULL, '
            f'created_at {created_at_type} NOT NULL, PRIMARY KEY (id), {check_sql}, '
            'FOREIGN KEY(user_id) REFERENCES "user" (id), '
            'FOREIGN KEY(payment_id) REFERENCES trade_payment (id), '
            'FOREIGN KEY(trade_id) REFERENCES trade (id), '
            'UNIQUE (payment_id, user_id, entry_type))'
        ))
        connection.execute(text('CREATE INDEX ix_point_ledger_user_id ON point_ledger (user_id)'))
        connection.execute(text('CREATE INDEX ix_point_ledger_payment_id ON point_ledger (payment_id)'))
        connection.execute(text('CREATE INDEX ix_point_ledger_trade_id ON point_ledger (trade_id)'))
        connection.execute(text('CREATE INDEX ix_point_ledger_created_at ON point_ledger (created_at)'))


def _create_point_ledger_table_with_extra_unique(engine):
    with engine.begin() as connection:
        connection.execute(text(
            'CREATE TABLE point_ledger ('
            'id VARCHAR(36) NOT NULL, user_id VARCHAR(36) NOT NULL, payment_id VARCHAR(36) NULL, '
            'trade_id VARCHAR(36) NULL, entry_type VARCHAR(30) NOT NULL, amount INTEGER NOT NULL, '
            'created_at DATETIME NOT NULL, PRIMARY KEY (id), '
            'UNIQUE (payment_id, user_id, entry_type), UNIQUE (user_id), '
            f'CHECK (amount >= 1 AND amount <= {MAX_POINT_BALANCE}), '
            "CHECK (entry_type IN ('ADMIN_GRANT', 'ESCROW_DEBIT', 'ESCROW_REFUND', 'SETTLEMENT_CREDIT')), "
            "CHECK ((entry_type = 'ADMIN_GRANT' AND payment_id IS NULL AND trade_id IS NULL) "
            "OR (entry_type IN ('ESCROW_DEBIT', 'ESCROW_REFUND', 'SETTLEMENT_CREDIT') "
            "AND payment_id IS NOT NULL AND trade_id IS NOT NULL)), "
            'FOREIGN KEY(user_id) REFERENCES "user" (id), '
            'FOREIGN KEY(payment_id) REFERENCES trade_payment (id), '
            'FOREIGN KEY(trade_id) REFERENCES trade (id))'
        ))
        connection.execute(text('CREATE INDEX ix_point_ledger_user_id ON point_ledger (user_id)'))
        connection.execute(text('CREATE INDEX ix_point_ledger_payment_id ON point_ledger (payment_id)'))
        connection.execute(text('CREATE INDEX ix_point_ledger_trade_id ON point_ledger (trade_id)'))
        connection.execute(text('CREATE INDEX ix_point_ledger_created_at ON point_ledger (created_at)'))
        connection.execute(text(
            "INSERT INTO point_ledger (id, user_id, payment_id, trade_id, entry_type, amount, created_at) "
            "VALUES ('ledger1', 'user1', NULL, NULL, 'ADMIN_GRANT', 5, '2026-01-01')"
        ))


def test_point_payment_migration_creates_tables_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'points.sqlite'}")
    _migration_base_tables(engine)

    first = migrate_point_payments_schema(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO point_wallet (id, user_id, balance, created_at, updated_at) "
            "VALUES ('wallet1', 'user1', 10, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        ))
    second = migrate_point_payments_schema(engine)

    assert first == 0
    assert second == 0
    assert payment_tables_are_compatible(engine) is True
    with engine.connect() as connection:
        assert set(inspect(engine).get_table_names()).issuperset({'point_wallet', 'trade_payment', 'point_ledger'})
        assert connection.execute(text('SELECT balance FROM point_wallet WHERE id = "wallet1"')).scalar_one() == 10
        assert connection.execute(text('PRAGMA foreign_key_check')).all() == []
    assert _unrelated_rows(engine) == [(1, 'preserve')]


def test_point_payment_migration_accepts_exact_model_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'exact_points.sqlite'}")
    _migration_base_tables(engine)
    PointWallet.__table__.create(bind=engine)
    TradePayment.__table__.create(bind=engine)
    PointLedger.__table__.create(bind=engine)

    assert table_is_compatible(engine, 'point_wallet') is True
    assert table_is_compatible(engine, 'trade_payment') is True
    assert table_is_compatible(engine, 'point_ledger') is True
    assert payment_tables_are_compatible(engine) is True
    assert migrate_point_payments_schema(engine) == 0


def test_point_payment_migration_completes_compatible_partial_state(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'partial_points.sqlite'}")
    _migration_base_tables(engine)
    PointWallet.__table__.create(bind=engine)

    result = migrate_point_payments_schema(engine)

    assert result == 0
    assert set(inspect(engine).get_table_names()).issuperset({'point_wallet', 'trade_payment', 'point_ledger'})


def test_point_payment_migration_rejects_incompatible_partial_before_creating_more_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_partial_points.sqlite'}")
    _migration_base_tables(engine)
    with engine.begin() as connection:
        connection.execute(text(
            'CREATE TABLE point_wallet ('
            'id VARCHAR(36) NOT NULL, user_id VARCHAR(36) NOT NULL, balance INTEGER NULL, '
            'created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, PRIMARY KEY (id), '
            'FOREIGN KEY(user_id) REFERENCES "user" (id), CHECK (balance >= 0))'
        ))
        connection.execute(text('CREATE UNIQUE INDEX ix_point_wallet_user_id ON point_wallet (user_id)'))
        connection.execute(text(
            "INSERT INTO point_wallet (id, user_id, balance, created_at, updated_at) "
            "VALUES ('badwallet', 'user1', 5, '2026-01-01', '2026-01-01')"
        ))
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'trade_payment' not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text('SELECT balance FROM point_wallet WHERE id = "badwallet"')).scalar_one() == 5
    assert _unrelated_rows(engine) == before_unrelated


def test_point_payment_migration_rejects_wallet_without_max_balance_check(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_wallet_check.sqlite'}")
    _migration_base_tables(engine)
    _create_legacy_point_wallet_table(engine)
    before_sql = _table_sql(engine, 'point_wallet')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert _table_sql(engine, 'point_wallet') == before_sql
    assert 'trade_payment' not in inspect(engine).get_table_names()
    assert _unrelated_rows(engine) == before_unrelated


def test_point_payment_migration_rejects_payment_without_amount_max_or_timestamp_consistency(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_payment_check.sqlite'}")
    _migration_base_tables(engine)
    PointWallet.__table__.create(bind=engine)
    _create_trade_payment_table_with_checks(engine, [
        'amount > 0',
        'buyer_id != seller_id',
        "status IN ('HELD', 'SETTLED', 'REFUNDED')",
    ])
    before_sql = _table_sql(engine, 'trade_payment')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert _table_sql(engine, 'trade_payment') == before_sql
    assert 'point_ledger' not in inspect(engine).get_table_names()
    assert _unrelated_rows(engine) == before_unrelated


def test_point_payment_migration_rejects_ledger_without_reference_consistency(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_ledger_check.sqlite'}")
    _migration_base_tables(engine)
    PointWallet.__table__.create(bind=engine)
    TradePayment.__table__.create(bind=engine)
    _create_point_ledger_table_with_checks(engine, [
        f'amount >= 1 AND amount <= {MAX_POINT_BALANCE}',
        "entry_type IN ('ADMIN_GRANT', 'ESCROW_DEBIT', 'ESCROW_REFUND', 'SETTLEMENT_CREDIT')",
    ])
    before_sql = _table_sql(engine, 'point_ledger')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert _table_sql(engine, 'point_ledger') == before_sql
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('created_at_type', ['INTEGER', 'TEXT'])
def test_point_payment_migration_rejects_point_ledger_incompatible_timestamp_type(tmp_path, created_at_type):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_ledger_timestamp.sqlite'}")
    _migration_base_tables(engine)
    PointWallet.__table__.create(bind=engine)
    TradePayment.__table__.create(bind=engine)
    _create_point_ledger_table_with_checks(engine, [
        f'amount >= 1 AND amount <= {MAX_POINT_BALANCE}',
        "entry_type IN ('ADMIN_GRANT', 'ESCROW_DEBIT', 'ESCROW_REFUND', 'SETTLEMENT_CREDIT')",
        (
            "(entry_type = 'ADMIN_GRANT' AND payment_id IS NULL AND trade_id IS NULL) "
            "OR (entry_type IN ('ESCROW_DEBIT', 'ESCROW_REFUND', 'SETTLEMENT_CREDIT') "
            "AND payment_id IS NOT NULL AND trade_id IS NOT NULL)"
        ),
    ], created_at_type=created_at_type)
    before_sql = _table_sql(engine, 'point_ledger')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert _table_sql(engine, 'point_ledger') == before_sql
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('kwargs', [
    {'extra_unique': 'UNIQUE (balance),'},
    {'extra_fk': 'FOREIGN KEY(user_id) REFERENCES wallet_parent (id),'},
    {'expected_fk': 'FOREIGN KEY(user_id) REFERENCES wallet_parent (id),'},
    {'extra_check': ", CHECK (user_id = 'user1')"},
    {'extra_check': ', CHECK (1 = 1)'},
])
def test_point_payment_migration_rejects_wallet_extra_constraints(tmp_path, kwargs):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_wallet_extra.sqlite'}")
    _migration_base_tables(engine)
    _create_point_wallet_table_with_extra_constraints(engine, **kwargs)
    before_sql = _table_sql(engine, 'point_wallet')
    before_rows = _table_rows(engine, 'point_wallet')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'trade_payment' not in inspect(engine).get_table_names()
    assert _table_sql(engine, 'point_wallet') == before_sql
    assert _table_rows(engine, 'point_wallet') == before_rows
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('fk_clause', [
    'FOREIGN KEY(user_id) REFERENCES "user" (id) ON DELETE CASCADE,',
    'FOREIGN KEY(user_id) REFERENCES "user" (id) ON DELETE SET NULL,',
    'FOREIGN KEY(user_id) REFERENCES "user" (id) ON UPDATE CASCADE,',
    'FOREIGN KEY(user_id) REFERENCES "user" (id) DEFERRABLE INITIALLY DEFERRED,',
])
def test_point_payment_migration_rejects_wallet_fk_actions_and_options(tmp_path, fk_clause):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_wallet_fk_options.sqlite'}")
    _migration_base_tables(engine)
    _create_point_wallet_table_with_extra_constraints(engine, expected_fk=fk_clause)
    before_sql = _table_sql(engine, 'point_wallet')
    before_rows = _table_rows(engine, 'point_wallet')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'trade_payment' not in inspect(engine).get_table_names()
    assert _table_sql(engine, 'point_wallet') == before_sql
    assert _table_rows(engine, 'point_wallet') == before_rows
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('index_sql', [
    'CREATE UNIQUE INDEX ix_point_wallet_user_id ON point_wallet (user_id) WHERE balance > 0',
    'CREATE UNIQUE INDEX ix_point_wallet_extra_partial ON point_wallet (balance) WHERE balance > 0',
])
def test_point_payment_migration_rejects_wallet_partial_unique_indexes(tmp_path, index_sql):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_wallet_partial_unique.sqlite'}")
    _migration_base_tables(engine)
    _create_point_wallet_table_with_extra_constraints(engine, create_required_index=False)
    with engine.begin() as connection:
        connection.execute(text(index_sql))
        if 'ix_point_wallet_user_id' not in index_sql:
            connection.execute(text('CREATE UNIQUE INDEX ix_point_wallet_user_id ON point_wallet (user_id)'))
    before_sql = _table_sql(engine, 'point_wallet')
    before_rows = _table_rows(engine, 'point_wallet')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'trade_payment' not in inspect(engine).get_table_names()
    assert _table_sql(engine, 'point_wallet') == before_sql
    assert _table_rows(engine, 'point_wallet') == before_rows
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('index_sql', [
    'CREATE UNIQUE INDEX ux_wallet_balance_expression ON point_wallet ((balance + 1))',
    'CREATE UNIQUE INDEX ux_wallet_user_lower ON point_wallet (lower(user_id))',
])
def test_point_payment_migration_rejects_additional_unique_expression_indexes(tmp_path, index_sql):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_wallet_unique_expression.sqlite'}")
    _migration_base_tables(engine)
    _create_point_wallet_table_with_extra_constraints(engine)
    with engine.begin() as connection:
        connection.execute(text(index_sql))
    before_sql = _table_sql(engine, 'point_wallet')
    before_rows = _table_rows(engine, 'point_wallet')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'trade_payment' not in inspect(engine).get_table_names()
    assert _table_sql(engine, 'point_wallet') == before_sql
    assert _table_rows(engine, 'point_wallet') == before_rows
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('index_sql', [
    'CREATE UNIQUE INDEX ix_point_wallet_user_id ON point_wallet (lower(user_id))',
    'CREATE UNIQUE INDEX ix_point_wallet_user_id ON point_wallet (user_id COLLATE NOCASE)',
    'CREATE UNIQUE INDEX ix_point_wallet_user_id ON point_wallet (user_id DESC)',
])
def test_point_payment_migration_rejects_required_wallet_index_wrong_semantics(tmp_path, index_sql):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_wallet_required_index.sqlite'}")
    _migration_base_tables(engine)
    _create_point_wallet_table_with_extra_constraints(engine, create_required_index=False)
    with engine.begin() as connection:
        connection.execute(text(index_sql))
    before_sql = _table_sql(engine, 'point_wallet')
    before_rows = _table_rows(engine, 'point_wallet')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'trade_payment' not in inspect(engine).get_table_names()
    assert _table_sql(engine, 'point_wallet') == before_sql
    assert _table_rows(engine, 'point_wallet') == before_rows
    assert _unrelated_rows(engine) == before_unrelated


def test_point_payment_migration_rejects_mixed_column_expression_composite_index(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_wallet_mixed_expression.sqlite'}")
    _migration_base_tables(engine)
    _create_point_wallet_table_with_extra_constraints(engine)
    with engine.begin() as connection:
        connection.execute(text('CREATE INDEX ix_wallet_mixed_expression ON point_wallet (user_id, (balance + 1))'))
    before_sql = _table_sql(engine, 'point_wallet')
    before_rows = _table_rows(engine, 'point_wallet')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'trade_payment' not in inspect(engine).get_table_names()
    assert _table_sql(engine, 'point_wallet') == before_sql
    assert _table_rows(engine, 'point_wallet') == before_rows
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('type_kwargs', [
    {'created_at_type': 'INTEGER'},
    {'created_at_type': 'TEXT'},
    {'updated_at_type': 'VARCHAR(30)'},
])
def test_point_payment_migration_rejects_wallet_incompatible_timestamp_types(tmp_path, type_kwargs):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_wallet_timestamp.sqlite'}")
    _migration_base_tables(engine)
    _create_point_wallet_table_with_extra_constraints(engine, **type_kwargs)
    before_sql = _table_sql(engine, 'point_wallet')
    before_rows = _table_rows(engine, 'point_wallet')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'trade_payment' not in inspect(engine).get_table_names()
    assert _table_sql(engine, 'point_wallet') == before_sql
    assert _table_rows(engine, 'point_wallet') == before_rows
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('type_kwargs', [
    {'id_type': 'BLOB(36)'},
    {'user_id_type': 'BLOB(36)'},
    {'id_type': 'CHAR(36)'},
    {'balance_type': 'BIGINT'},
    {'id_type': 'VARCHAR(35)'},
])
def test_point_payment_migration_rejects_wallet_incompatible_declared_types(tmp_path, type_kwargs):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_wallet_declared_type.sqlite'}")
    _migration_base_tables(engine)
    _create_point_wallet_table_with_extra_constraints(engine, **type_kwargs)
    before_sql = _table_sql(engine, 'point_wallet')
    before_rows = _table_rows(engine, 'point_wallet')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'trade_payment' not in inspect(engine).get_table_names()
    assert _table_sql(engine, 'point_wallet') == before_sql
    assert _table_rows(engine, 'point_wallet') == before_rows
    assert _unrelated_rows(engine) == before_unrelated


def test_point_payment_migration_rejects_trade_payment_extra_unique_and_check(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_payment_unique_check.sqlite'}")
    _migration_base_tables(engine)
    PointWallet.__table__.create(bind=engine)
    _create_trade_payment_table_with_checks(engine, [
        f'amount >= 1 AND amount <= {MAX_POINT_BALANCE}',
        'buyer_id != seller_id',
        "status IN ('HELD', 'SETTLED', 'REFUNDED')",
        (
            "(status = 'HELD' AND settled_at IS NULL AND refunded_at IS NULL) "
            "OR (status = 'SETTLED' AND settled_at IS NOT NULL AND refunded_at IS NULL) "
            "OR (status = 'REFUNDED' AND refunded_at IS NOT NULL AND settled_at IS NULL)"
        ),
        'trade_id IS NOT NULL',
    ])
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO trade_payment "
            "(id, trade_id, buyer_id, seller_id, amount, status, created_at, updated_at, settled_at, refunded_at) "
            "VALUES ('payment1', 'trade1', 'user2', 'user1', 1000, 'HELD', "
            "'2026-01-01', '2026-01-01', NULL, NULL)"
        ))
    before_sql = _table_sql(engine, 'trade_payment')
    before_rows = _table_rows(engine, 'trade_payment')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'point_ledger' not in inspect(engine).get_table_names()
    assert _table_sql(engine, 'trade_payment') == before_sql
    assert _table_rows(engine, 'trade_payment') == before_rows
    assert _unrelated_rows(engine) == before_unrelated


def test_point_payment_migration_rejects_trade_payment_extra_unique_status(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_payment_unique.sqlite'}")
    _migration_base_tables(engine)
    PointWallet.__table__.create(bind=engine)
    _create_trade_payment_table_with_extra_unique(engine)
    before_sql = _table_sql(engine, 'trade_payment')
    before_rows = _table_rows(engine, 'trade_payment')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'point_ledger' not in inspect(engine).get_table_names()
    assert _table_sql(engine, 'trade_payment') == before_sql
    assert _table_rows(engine, 'trade_payment') == before_rows
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('trade_index_sql', [
    'CREATE UNIQUE INDEX ix_trade_payment_trade_id ON trade_payment (trade_id) WHERE status = "HELD"',
])
def test_point_payment_migration_rejects_trade_payment_partial_unique_index(tmp_path, trade_index_sql):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_payment_partial_unique.sqlite'}")
    _migration_base_tables(engine)
    PointWallet.__table__.create(bind=engine)
    _create_trade_payment_table_with_checks(engine, [
        f'amount >= 1 AND amount <= {MAX_POINT_BALANCE}',
        'buyer_id != seller_id',
        "status IN ('HELD', 'SETTLED', 'REFUNDED')",
        (
            "(status = 'HELD' AND settled_at IS NULL AND refunded_at IS NULL) "
            "OR (status = 'SETTLED' AND settled_at IS NOT NULL AND refunded_at IS NULL) "
            "OR (status = 'REFUNDED' AND refunded_at IS NOT NULL AND settled_at IS NULL)"
        ),
    ], trade_index_sql=trade_index_sql)
    before_sql = _table_sql(engine, 'trade_payment')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'point_ledger' not in inspect(engine).get_table_names()
    assert _table_sql(engine, 'trade_payment') == before_sql
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('buyer_index_sql', [
    'CREATE INDEX ix_trade_payment_buyer_id ON trade_payment (buyer_id) WHERE amount > 0',
    'CREATE UNIQUE INDEX ix_trade_payment_buyer_id ON trade_payment (buyer_id)',
])
def test_point_payment_migration_rejects_required_named_index_predicate_or_wrong_uniqueness(tmp_path, buyer_index_sql):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_payment_named_index.sqlite'}")
    _migration_base_tables(engine)
    PointWallet.__table__.create(bind=engine)
    _create_trade_payment_table_with_checks(engine, [
        f'amount >= 1 AND amount <= {MAX_POINT_BALANCE}',
        'buyer_id != seller_id',
        "status IN ('HELD', 'SETTLED', 'REFUNDED')",
        (
            "(status = 'HELD' AND settled_at IS NULL AND refunded_at IS NULL) "
            "OR (status = 'SETTLED' AND settled_at IS NOT NULL AND refunded_at IS NULL) "
            "OR (status = 'REFUNDED' AND refunded_at IS NOT NULL AND settled_at IS NULL)"
        ),
    ], buyer_index_sql=buyer_index_sql)
    before_sql = _table_sql(engine, 'trade_payment')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'point_ledger' not in inspect(engine).get_table_names()
    assert _table_sql(engine, 'trade_payment') == before_sql
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('type_kwargs', [
    {'created_at_type': 'INTEGER'},
    {'created_at_type': 'TEXT'},
    {'updated_at_type': 'VARCHAR(30)'},
    {'settled_at_type': 'INTEGER'},
    {'refunded_at_type': 'TEXT'},
])
def test_point_payment_migration_rejects_trade_payment_incompatible_timestamp_types(tmp_path, type_kwargs):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_payment_timestamp.sqlite'}")
    _migration_base_tables(engine)
    PointWallet.__table__.create(bind=engine)
    _create_trade_payment_table_with_checks(engine, [
        f'amount >= 1 AND amount <= {MAX_POINT_BALANCE}',
        'buyer_id != seller_id',
        "status IN ('HELD', 'SETTLED', 'REFUNDED')",
        (
            "(status = 'HELD' AND settled_at IS NULL AND refunded_at IS NULL) "
            "OR (status = 'SETTLED' AND settled_at IS NOT NULL AND refunded_at IS NULL) "
            "OR (status = 'REFUNDED' AND refunded_at IS NOT NULL AND settled_at IS NULL)"
        ),
    ], **type_kwargs)
    before_sql = _table_sql(engine, 'trade_payment')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'point_ledger' not in inspect(engine).get_table_names()
    assert _table_sql(engine, 'trade_payment') == before_sql
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('type_kwargs', [
    {'status_type': 'BLOB(20)'},
    {'amount_type': 'NUMERIC'},
    {'id_type': 'VARCHAR(35)'},
    {'status_type': 'VARCHAR(21)'},
])
def test_point_payment_migration_rejects_trade_payment_incompatible_declared_types(tmp_path, type_kwargs):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_payment_declared_type.sqlite'}")
    _migration_base_tables(engine)
    PointWallet.__table__.create(bind=engine)
    _create_trade_payment_table_with_checks(engine, [
        f'amount >= 1 AND amount <= {MAX_POINT_BALANCE}',
        'buyer_id != seller_id',
        "status IN ('HELD', 'SETTLED', 'REFUNDED')",
        (
            "(status = 'HELD' AND settled_at IS NULL AND refunded_at IS NULL) "
            "OR (status = 'SETTLED' AND settled_at IS NOT NULL AND refunded_at IS NULL) "
            "OR (status = 'REFUNDED' AND refunded_at IS NOT NULL AND settled_at IS NULL)"
        ),
    ], **type_kwargs)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO trade_payment "
            "(id, trade_id, buyer_id, seller_id, amount, status, created_at, updated_at, settled_at, refunded_at) "
            "VALUES ('payment1', 'trade1', 'user2', 'user1', 1000, 'HELD', "
            "'2026-01-01', '2026-01-01', NULL, NULL)"
        ))
    before_sql = _table_sql(engine, 'trade_payment')
    before_rows = _table_rows(engine, 'trade_payment')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert 'point_ledger' not in inspect(engine).get_table_names()
    assert _table_sql(engine, 'trade_payment') == before_sql
    assert _table_rows(engine, 'trade_payment') == before_rows
    assert _unrelated_rows(engine) == before_unrelated


def test_point_payment_migration_rejects_point_ledger_extra_unique_user_id(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_ledger_unique.sqlite'}")
    _migration_base_tables(engine)
    PointWallet.__table__.create(bind=engine)
    TradePayment.__table__.create(bind=engine)
    _create_point_ledger_table_with_extra_unique(engine)
    before_sql = _table_sql(engine, 'point_ledger')
    before_rows = _table_rows(engine, 'point_ledger')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert _table_sql(engine, 'point_ledger') == before_sql
    assert _table_rows(engine, 'point_ledger') == before_rows
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('type_kwargs', [
    {'entry_type_type': 'BLOB(30)'},
    {'amount_type': 'NUMERIC'},
    {'id_type': 'CHAR(36)'},
    {'entry_type_type': 'VARCHAR(31)'},
])
def test_point_payment_migration_rejects_point_ledger_incompatible_declared_types(tmp_path, type_kwargs):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_ledger_declared_type.sqlite'}")
    _migration_base_tables(engine)
    PointWallet.__table__.create(bind=engine)
    TradePayment.__table__.create(bind=engine)
    _create_point_ledger_table_with_checks(engine, [
        f'amount >= 1 AND amount <= {MAX_POINT_BALANCE}',
        "entry_type IN ('ADMIN_GRANT', 'ESCROW_DEBIT', 'ESCROW_REFUND', 'SETTLEMENT_CREDIT')",
        (
            "(entry_type = 'ADMIN_GRANT' AND payment_id IS NULL AND trade_id IS NULL) "
            "OR (entry_type IN ('ESCROW_DEBIT', 'ESCROW_REFUND', 'SETTLEMENT_CREDIT') "
            "AND payment_id IS NOT NULL AND trade_id IS NOT NULL)"
        ),
    ], **type_kwargs)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO point_ledger (id, user_id, payment_id, trade_id, entry_type, amount, created_at) "
            "VALUES ('ledger1', 'user1', NULL, NULL, 'ADMIN_GRANT', 5, '2026-01-01')"
        ))
    before_sql = _table_sql(engine, 'point_ledger')
    before_rows = _table_rows(engine, 'point_ledger')
    before_unrelated = _unrelated_rows(engine)

    result = migrate_point_payments_schema(engine)

    assert result == 2
    assert _table_sql(engine, 'point_ledger') == before_sql
    assert _table_rows(engine, 'point_ledger') == before_rows
    assert _unrelated_rows(engine) == before_unrelated


def test_point_payment_migration_rejects_unsupported_database():
    class FakeDialect:
        name = 'postgresql'

    class FakeEngine:
        dialect = FakeDialect()

    assert migrate_point_payments_schema(FakeEngine()) == 2
    assert payment_tables_are_compatible(FakeEngine()) is False
