import pytest

from market import create_app
from market.extensions import db
from market.models import Product, User


TEST_PASSWORD = 'TestPassword123!'
JPEG_BYTES = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00' + b'\x00' * 16


@pytest.fixture
def mypage_upload_dir(app, tmp_path):
    upload_dir = tmp_path / 'uploads' / 'products'
    app.config['PRODUCT_IMAGE_UPLOAD_DIR'] = str(upload_dir)
    return upload_dir


@pytest.fixture
def csrf_app(tmp_path):
    app = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': True,
        'RATELIMIT_ENABLED': False,
        'SECRET_KEY': 'csrf-test-secret-key',
        'PRODUCT_IMAGE_UPLOAD_DIR': str(tmp_path / 'uploads' / 'products'),
    })

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def csrf_client(csrf_app):
    return csrf_app.test_client()


def _create_user(username, role=User.ROLE_USER, is_active=True):
    user = User(username=username, role=role, is_active=is_active)
    user.set_password(TEST_PASSWORD)
    db.session.add(user)
    db.session.commit()
    return user.id


def _create_product(seller_id, title='Owned Product', price=1000, status=Product.STATUS_SELLING):
    product = Product(
        seller_id=seller_id,
        title=title,
        description=f'{title} description',
        price=price,
        status=status,
    )
    db.session.add(product)
    db.session.commit()
    return product.id


def _product_count(app):
    with app.app_context():
        return Product.query.count()


def test_unauthenticated_access_to_mypage_is_redirected_to_login(client):
    response = client.get('/mypage')

    assert response.status_code == 302
    assert '/login' in response.headers['Location']


def test_authenticated_active_user_can_access_mypage(client, auth_helper, normal_user, mypage_upload_dir):
    auth_helper.login('testuser')

    response = client.get('/mypage')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '마이페이지' in body
    assert 'testuser' in body
    assert '/product/new' in body


def test_banned_or_inactive_user_cannot_access_mypage(client, inactive_user, mypage_upload_dir):
    with client.session_transaction() as sess:
        sess['user_id'] = inactive_user

    response = client.get('/mypage')

    assert response.status_code == 302
    assert '/login' in response.headers['Location']
    with client.session_transaction() as sess:
        assert 'user_id' not in sess


def test_user_sees_own_product_but_not_another_users_product(
    client,
    app,
    auth_helper,
    normal_user,
    second_user,
    mypage_upload_dir,
):
    with app.app_context():
        own_product_id = _create_product(normal_user, title='My Owned Product', price=1111)
        _create_product(second_user, title='Other User Product', price=2222)

    auth_helper.login('testuser')
    response = client.get('/mypage')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'My Owned Product' in body
    assert '1111원' in body
    assert Product.STATUS_SELLING in body
    assert f'/product/{own_product_id}' in body
    assert 'Other User Product' not in body
    assert '2222원' not in body


def test_empty_state_is_rendered_without_other_users_products(
    client,
    app,
    auth_helper,
    normal_user,
    second_user,
    mypage_upload_dir,
):
    with app.app_context():
        _create_product(second_user, title='Only Other User Product')

    auth_helper.login('testuser')
    response = client.get('/mypage')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '등록한 상품이 없습니다.' in body
    assert '/product/new' in body
    assert 'Only Other User Product' not in body


def test_product_management_links_and_post_delete_form_are_present(
    client,
    app,
    auth_helper,
    normal_user,
    mypage_upload_dir,
):
    with app.app_context():
        product_id = _create_product(normal_user, title='Managed Product')

    auth_helper.login('testuser')
    response = client.get('/mypage')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f'href="/product/{product_id}"' in body
    assert f'href="/product/{product_id}/edit"' in body
    assert f'action="/product/{product_id}/delete"' in body
    assert 'method="post"' in body
    assert 'method="get"' not in body.lower().split(f'action="/product/{product_id}/delete"')[0][-80:].lower()


def test_delete_form_includes_csrf_token_when_csrf_is_enabled(csrf_app, csrf_client):
    with csrf_app.app_context():
        user_id = _create_user('csrfuser')
        product_id = _create_product(user_id, title='CSRF Product')

    with csrf_client.session_transaction() as sess:
        sess['user_id'] = user_id

    response = csrf_client.get('/mypage')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f'action="/product/{product_id}/delete"' in body
    assert 'name="csrf_token"' in body
    assert 'value=""' not in body


def test_direct_edit_and_delete_of_another_users_product_remain_denied(
    client,
    app,
    auth_helper,
    normal_user,
    second_user,
    product,
    mypage_upload_dir,
):
    auth_helper.login('seconduser')

    edit_response = client.get(f'/product/{product}/edit')
    delete_response = client.post(f'/product/{product}/delete')

    assert edit_response.status_code == 302
    assert edit_response.headers['Location'].endswith('/dashboard')
    assert delete_response.status_code == 302
    assert delete_response.headers['Location'].endswith('/dashboard')
    with app.app_context():
        existing = db.session.get(Product, product)
        assert existing is not None
        assert existing.seller_id == normal_user
        assert existing.seller_id != second_user


def test_product_image_uses_controlled_route_and_missing_image_uses_placeholder(
    client,
    app,
    auth_helper,
    normal_user,
    mypage_upload_dir,
):
    with app.app_context():
        image_product_id = _create_product(normal_user, title='Image Product')
        no_image_product_id = _create_product(normal_user, title='No Image Product')

    mypage_upload_dir.mkdir(parents=True, exist_ok=True)
    (mypage_upload_dir / f'{image_product_id}.jpg').write_bytes(JPEG_BYTES)

    auth_helper.login('testuser')
    response = client.get('/mypage')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f'src="/product/{image_product_id}/image"' in body
    assert f'/product/{no_image_product_id}/image' not in body
    assert '이미지 없음' in body
    assert str(mypage_upload_dir) not in body


def test_mypage_does_not_expose_sensitive_fields(client, app, auth_helper, normal_user, mypage_upload_dir):
    with app.app_context():
        _create_product(normal_user, title='Sensitive Check Product')

    auth_helper.login('testuser')
    response = client.get('/mypage')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    forbidden = [
        'password_hash',
        'TestPassword123!',
        'ROLE_ADMIN',
        'adminuser',
        'is_active',
        'session',
        'csrf-test-secret-key',
    ]
    for value in forbidden:
        assert value not in body


def test_existing_product_create_edit_delete_and_ownership_behavior_remains_intact(
    client,
    app,
    auth_helper,
    normal_user,
    second_user,
    mypage_upload_dir,
):
    auth_helper.login('testuser')
    create_response = client.post('/product/new', data={
        'title': 'Created From Mypage Test',
        'description': 'Created description',
        'price': '3000',
        'seller_id': second_user,
    })

    assert create_response.status_code == 302
    with app.app_context():
        created = Product.query.filter_by(title='Created From Mypage Test').one()
        product_id = created.id
        assert created.seller_id == normal_user
        assert created.seller_id != second_user

    edit_response = client.post(f'/product/{product_id}/edit', data={
        'title': 'Edited From Mypage Test',
        'description': 'Edited description',
        'price': '4000',
        'status': Product.STATUS_SELLING,
        'seller_id': second_user,
    })

    assert edit_response.status_code == 302
    with app.app_context():
        edited = db.session.get(Product, product_id)
        assert edited.title == 'Edited From Mypage Test'
        assert edited.description == 'Edited description'
        assert edited.price == 4000
        assert edited.seller_id == normal_user

    delete_response = client.post(f'/product/{product_id}/delete')

    assert delete_response.status_code == 302
    with app.app_context():
        assert db.session.get(Product, product_id) is None
    assert _product_count(app) == 0
