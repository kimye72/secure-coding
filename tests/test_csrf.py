import pytest
import re
from market import create_app
from market.extensions import db

def extract_csrf_token(response):
    html = response.get_data(as_text=True)
    match = re.search(
        r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']',
        html,
    )
    assert match is not None, 'csrf_token input was not found'
    return match.group(1)

@pytest.fixture
def csrf_app():
    app = create_app(test_config={
        'TESTING': True,
        'WTF_CSRF_ENABLED': True,
        'RATELIMIT_ENABLED': False,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'SECRET_KEY': 'csrf-secret'
    })
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

@pytest.fixture
def csrf_client(csrf_app):
    return csrf_app.test_client()

def test_login_without_csrf_token(csrf_client):
    response = csrf_client.post('/login', data={'username': 'test', 'password': 'password'})
    assert response.status_code == 400
    assert b'\xec\x9a\x94\xec\xb2\xad\xec\x9d\x84 \xed\x99\x95\xec\x9d\xb8\xed\x95\xa0 \xec\x88\x98 \xec\x97\x86\xec\x8a\xb5\xeb\x8b\x88\xeb\x8b\xa4.' in response.data # "요청을 확인할 수 없습니다."
    assert b'CSRFError' not in response.data # No raw exception details

def test_login_has_csrf_token(csrf_client):
    response = csrf_client.get('/login')
    assert response.status_code == 200
    assert b'csrf_token' in response.data

def test_login_with_valid_csrf_token(csrf_client, csrf_app):
    from market.models import User
    from tests.conftest import TEST_PASSWORD
    with csrf_app.app_context():
        u = User(username='testuser', role=User.ROLE_USER, is_active=True)
        u.set_password(TEST_PASSWORD)
        db.session.add(u)
        db.session.commit()

    # Get the form to extract token
    get_response = csrf_client.get('/login')
    csrf_token = extract_csrf_token(get_response)

    # Submit with token
    post_response = csrf_client.post('/login', data={
        'csrf_token': csrf_token,
        'username': 'testuser',
        'password': TEST_PASSWORD
    })
    assert post_response.status_code == 302
    assert post_response.headers['Location'].endswith('/dashboard')

def test_logout_without_csrf_token(csrf_client):
    # Try logout without token
    response = csrf_client.post('/logout')
    assert response.status_code == 400

def test_logout_with_valid_csrf_token(csrf_client, csrf_app):
    from market.models import User
    from tests.conftest import TEST_PASSWORD
    with csrf_app.app_context():
        u = User(username='testuser', role=User.ROLE_USER, is_active=True)
        u.set_password(TEST_PASSWORD)
        db.session.add(u)
        db.session.commit()
        user_id = u.id

    with csrf_client.session_transaction() as sess:
        sess['user_id'] = user_id

    dash_response = csrf_client.get('/dashboard')
    assert dash_response.status_code == 200
    logout_token = extract_csrf_token(dash_response)

    logout_response = csrf_client.post('/logout', data={'csrf_token': logout_token})
    assert logout_response.status_code == 302

    with csrf_client.session_transaction() as sess:
        assert 'user_id' not in sess
