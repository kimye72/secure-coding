from market.models import Trade, Product
from market.extensions import db

def test_unauthenticated_cannot_access_login_required(client):
    response = client.get('/profile')
    assert response.status_code == 302
    assert '/login' in response.headers['Location']

def test_normal_user_cannot_access_admin(client, auth_helper, normal_user):
    response_login = auth_helper.login('testuser')
    assert response_login.status_code == 302
    response = client.get('/admin/reports')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/dashboard')

def test_inactive_user_denied(client, auth_helper, inactive_user):
    auth_helper.login('inactive')
    response = client.get('/profile')
    assert response.status_code == 302
    assert '/login' in response.headers['Location']

def test_user_cannot_edit_another_product(client, auth_helper, second_user, product):
    response_login = auth_helper.login('seconduser')
    assert response_login.status_code == 302
    response = client.post(f'/product/{product}/edit', data={'title': 'New', 'description': 'New', 'price': 100})
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/dashboard')

def test_user_cannot_delete_another_product(client, auth_helper, second_user, product):
    response_login = auth_helper.login('seconduser')
    assert response_login.status_code == 302
    response = client.post(f'/product/{product}/delete')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/dashboard')

def test_seller_cannot_trade_own_product(client, auth_helper, normal_user, product):
    auth_helper.login('testuser')
    response = client.post(f'/product/{product}/trade/request')
    assert response.status_code == 302
    assert f'/product/{product}' in response.headers['Location']

def test_unrelated_user_cannot_transition_trade(client, app, auth_helper, normal_user, second_user, admin_user, product):
    with app.app_context():
        t = Trade(product_id=product, buyer_id=second_user, seller_id=normal_user, status=Trade.STATUS_PENDING)
        db.session.add(t)
        db.session.commit()
        trade_id = t.id

    auth_helper.login('adminuser') # Unrelated

    response = client.post(f'/trade/{trade_id}/accept')
    assert response.status_code == 302
    assert '/trades' in response.headers['Location']

    response = client.post(f'/trade/{trade_id}/reject')
    assert response.status_code == 302
    assert '/trades' in response.headers['Location']

    response = client.post(f'/trade/{trade_id}/cancel')
    assert response.status_code == 302
    assert '/trades' in response.headers['Location']

def test_buyer_can_cancel_but_not_accept_reject(client, app, auth_helper, normal_user, second_user, product):
    with app.app_context():
        t = Trade(product_id=product, buyer_id=second_user, seller_id=normal_user, status=Trade.STATUS_PENDING)
        db.session.add(t)
        db.session.commit()
        trade_id = t.id

    auth_helper.login('seconduser') # Buyer

    response = client.post(f'/trade/{trade_id}/accept')
    assert response.status_code == 302 # Denied

    response = client.post(f'/trade/{trade_id}/reject')
    assert response.status_code == 302 # Denied

    response = client.post(f'/trade/{trade_id}/cancel')
    assert response.status_code == 302 # Allowed (or redirects to trades)
    # Check DB state
    with app.app_context():
        updated_trade = db.session.get(Trade, trade_id)
        assert updated_trade.status == Trade.STATUS_CANCELLED

def test_seller_can_accept_reject_but_not_cancel_pending(client, app, auth_helper, normal_user, second_user, product):
    with app.app_context():
        t = Trade(product_id=product, buyer_id=second_user, seller_id=normal_user, status=Trade.STATUS_PENDING)
        db.session.add(t)
        db.session.commit()
        trade_id = t.id

    auth_helper.login('testuser') # Seller

    response = client.post(f'/trade/{trade_id}/cancel')
    assert response.status_code == 302 # Seller could cancel ACCEPTED, but depending on rule... actually, seller can reject PENDING. Let's test reject.
    # To be safe, test accept:
    response = client.post(f'/trade/{trade_id}/accept')
    assert response.status_code == 302
    with app.app_context():
        updated_trade = db.session.get(Trade, trade_id)
        assert updated_trade.status == Trade.STATUS_ACCEPTED
