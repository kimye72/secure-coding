import pytest
from market import create_app
from market.extensions import db
from market.models import User, Product, Trade, Report
from werkzeug.security import generate_password_hash

TEST_PASSWORD = 'TestPassword123!'
@pytest.fixture
def app():
    app = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': False,
        'RATELIMIT_ENABLED': False,
        'SECRET_KEY': 'test-secret-key-1234'
    })

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

@pytest.fixture
def auth_helper(client):
    class AuthActions:
        def __init__(self, client):
            self._client = client

        def login(self, username='testuser', password=TEST_PASSWORD):
            return self._client.post(
                '/login',
                data={
                    'username': username,
                    'password': password,
                },
            )

        def logout(self):
            return self._client.post('/logout')

    return AuthActions(client)

@pytest.fixture
def normal_user(app):
    with app.app_context():
        u = User(username='testuser', role=User.ROLE_USER, is_active=True)
        u.set_password(TEST_PASSWORD)
        db.session.add(u)
        db.session.commit()
        return u.id

@pytest.fixture
def second_user(app):
    with app.app_context():
        u = User(username='seconduser', role=User.ROLE_USER, is_active=True)
        u.set_password(TEST_PASSWORD)
        db.session.add(u)
        db.session.commit()
        return u.id

@pytest.fixture
def admin_user(app):
    with app.app_context():
        u = User(username='adminuser', role=User.ROLE_ADMIN, is_active=True)
        u.set_password(TEST_PASSWORD)
        db.session.add(u)
        db.session.commit()
        return u.id

@pytest.fixture
def inactive_user(app):
    with app.app_context():
        u = User(username='inactive', role=User.ROLE_USER, is_active=False)
        u.set_password(TEST_PASSWORD)
        db.session.add(u)
        db.session.commit()
        return u.id

@pytest.fixture
def product(app, normal_user):
    with app.app_context():
        p = Product(seller_id=normal_user, title='Test Product', description='Desc', price=1000, status=Product.STATUS_SELLING)
        db.session.add(p)
        db.session.commit()
        return p.id
