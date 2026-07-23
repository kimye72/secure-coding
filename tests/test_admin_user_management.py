import uuid

from market.extensions import db
from market.models import Product, User

TEST_PASSWORD = 'TestPassword123!'


def _create_admin(app, username='secondadmin'):
    with app.app_context():
        admin = User(username=username, role=User.ROLE_ADMIN, is_active=True)
        admin.set_password(TEST_PASSWORD)
        db.session.add(admin)
        db.session.commit()
        return admin.id


def test_unauthenticated_user_cannot_access_admin_users(client):
    response = client.get('/admin/users')

    assert response.status_code == 302
    assert '/login' in response.headers['Location']


def test_ordinary_user_cannot_access_admin_users(client, auth_helper, normal_user):
    auth_helper.login('testuser')

    response = client.get('/admin/users')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/dashboard')


def test_administrator_can_access_admin_users(client, auth_helper, normal_user, admin_user):
    auth_helper.login('adminuser')

    response = client.get('/admin/users')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'testuser' in body
    assert 'adminuser' in body
    assert User.ROLE_USER in body
    assert User.ROLE_ADMIN in body
    assert 'password_hash' not in body


def test_ordinary_user_cannot_call_ban_or_unban(client, app, auth_helper, normal_user, second_user):
    auth_helper.login('testuser')

    ban_response = client.post(f'/admin/user/{second_user}/ban')
    unban_response = client.post(f'/admin/user/{second_user}/unban')

    assert ban_response.status_code == 302
    assert ban_response.headers['Location'].endswith('/dashboard')
    assert unban_response.status_code == 302
    assert unban_response.headers['Location'].endswith('/dashboard')

    with app.app_context():
        target = db.session.get(User, second_user)
        assert target.is_active is True


def test_administrator_can_ban_active_ordinary_user(client, app, auth_helper, normal_user, admin_user):
    auth_helper.login('adminuser')

    response = client.post(f'/admin/user/{normal_user}/ban')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/admin/users')

    with app.app_context():
        target = db.session.get(User, normal_user)
        assert target is not None
        assert target.is_active is False
        assert target.role == User.ROLE_USER


def test_banned_user_cannot_successfully_log_in(client, app, auth_helper, normal_user, admin_user):
    auth_helper.login('adminuser')
    client.post(f'/admin/user/{normal_user}/ban')
    auth_helper.logout()

    response = auth_helper.login('testuser')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/login')
    with client.session_transaction() as sess:
        assert 'user_id' not in sess


def test_banned_user_with_old_session_is_denied(client, app, auth_helper, normal_user, admin_user):
    auth_helper.login('testuser')

    with app.app_context():
        target = db.session.get(User, normal_user)
        target.is_active = False
        db.session.commit()

    response = client.get('/profile')

    assert response.status_code == 302
    assert '/login' in response.headers['Location']
    with client.session_transaction() as sess:
        assert 'user_id' not in sess


def test_administrator_can_unban_user(client, app, auth_helper, inactive_user, admin_user):
    auth_helper.login('adminuser')

    response = client.post(f'/admin/user/{inactive_user}/unban')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/admin/users')

    with app.app_context():
        target = db.session.get(User, inactive_user)
        assert target.is_active is True
        assert target.role == User.ROLE_USER


def test_administrator_cannot_ban_self(client, app, auth_helper, admin_user):
    auth_helper.login('adminuser')

    response = client.post(f'/admin/user/{admin_user}/ban')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/admin/users')

    with app.app_context():
        admin = db.session.get(User, admin_user)
        assert admin.is_active is True
        assert admin.role == User.ROLE_ADMIN


def test_administrator_cannot_ban_another_administrator(client, app, auth_helper, admin_user):
    second_admin_id = _create_admin(app)
    auth_helper.login('adminuser')

    response = client.post(f'/admin/user/{second_admin_id}/ban')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/admin/users')

    with app.app_context():
        second_admin = db.session.get(User, second_admin_id)
        assert second_admin.is_active is True
        assert second_admin.role == User.ROLE_ADMIN


def test_administrator_cannot_unban_another_administrator(client, app, auth_helper, admin_user):
    second_admin_id = _create_admin(app)
    auth_helper.login('adminuser')

    response = client.post(f'/admin/user/{second_admin_id}/unban')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/admin/users')

    with app.app_context():
        second_admin = db.session.get(User, second_admin_id)
        assert second_admin.is_active is True
        assert second_admin.role == User.ROLE_ADMIN


def test_invalid_and_nonexistent_user_ids_are_handled_safely(client, auth_helper, admin_user):
    auth_helper.login('adminuser')

    invalid_response = client.post('/admin/user/not-a-uuid/ban')
    nonexistent_response = client.post(f'/admin/user/{uuid.uuid4()}/ban')

    assert invalid_response.status_code == 302
    assert invalid_response.headers['Location'].endswith('/admin/users')
    assert nonexistent_response.status_code == 302
    assert nonexistent_response.headers['Location'].endswith('/admin/users')


def test_get_requests_to_ban_and_unban_return_405(client, auth_helper, normal_user, admin_user):
    auth_helper.login('adminuser')

    ban_response = client.get(f'/admin/user/{normal_user}/ban')
    unban_response = client.get(f'/admin/user/{normal_user}/unban')

    assert ban_response.status_code == 405
    assert unban_response.status_code == 405


def test_ban_and_unban_do_not_change_target_role(client, app, auth_helper, normal_user, admin_user):
    auth_helper.login('adminuser')

    client.post(f'/admin/user/{normal_user}/ban')
    client.post(f'/admin/user/{normal_user}/unban')

    with app.app_context():
        target = db.session.get(User, normal_user)
        assert target.role == User.ROLE_USER
        assert target.is_active is True


def test_repeated_ban_and_unban_are_safe(client, app, auth_helper, normal_user, admin_user):
    auth_helper.login('adminuser')

    first_ban = client.post(f'/admin/user/{normal_user}/ban')
    second_ban = client.post(f'/admin/user/{normal_user}/ban')
    first_unban = client.post(f'/admin/user/{normal_user}/unban')
    second_unban = client.post(f'/admin/user/{normal_user}/unban')

    assert first_ban.status_code == 302
    assert second_ban.status_code == 302
    assert first_unban.status_code == 302
    assert second_unban.status_code == 302

    with app.app_context():
        target = db.session.get(User, normal_user)
        assert target.is_active is True
        assert target.role == User.ROLE_USER


def test_ban_does_not_delete_target_user_or_existing_product(client, app, auth_helper, normal_user, admin_user, product):
    auth_helper.login('adminuser')

    response = client.post(f'/admin/user/{normal_user}/ban')

    assert response.status_code == 302

    with app.app_context():
        target = db.session.get(User, normal_user)
        existing_product = db.session.get(Product, product)
        assert target is not None
        assert target.is_active is False
        assert existing_product is not None
        assert existing_product.seller_id == normal_user
