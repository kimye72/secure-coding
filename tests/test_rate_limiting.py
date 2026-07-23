import pytest
from market import create_app
from market.extensions import db

@pytest.fixture
def rate_app():
    app = create_app(test_config={
        'TESTING': True,
        'WTF_CSRF_ENABLED': False,
        'RATELIMIT_ENABLED': True,
        'RATELIMIT_STORAGE_URI': 'memory://',
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'SECRET_KEY': 'rate-secret'
    })
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

@pytest.fixture
def rate_client(rate_app):
    return rate_app.test_client()

def test_login_rate_limit(rate_client):
    # First 5 attempts should not be 429
    for _ in range(5):
        response = rate_client.post('/login', data={'username': 'test', 'password': 'pwd'})
        assert response.status_code != 429

    # 6th attempt should be 429
    response = rate_client.post('/login', data={'username': 'test', 'password': 'pwd'})
    assert response.status_code == 429

    # Check Korean message
    assert b'\xec\x9a\x94\xec\xb2\xad\xec\x9d\xb4 \xeb\x84\x88\xeb\xac\xb4 \xeb\xa7\x8e\xec\x8a\xb5\xeb\x8b\x88\xeb\x8b\xa4.' in response.data # "요청이 너무 많습니다."

    # Check no exposed details
    body = response.data.decode('utf-8').lower()
    assert '127.0.0.1' not in body
    assert 'ip:127.0.0.1' not in body
    assert 'user:' not in body
    assert 'memory://' not in body
    assert 'traceback' not in body
    assert 'storage key' not in body
    assert 'ratelimit_storage' not in body
    assert 'exception' not in body

    # GET /login still returns 200
    response_get = rate_client.get('/login')
    assert response_get.status_code == 200
