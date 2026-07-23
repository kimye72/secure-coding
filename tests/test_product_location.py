from io import BytesIO

import pytest
from sqlalchemy import create_engine, inspect, text

from market.extensions import db
from market.models import Product
from scripts.add_product_trade_location import add_trade_location_column


JPEG_BYTES = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00' + b'\x00' * 16


@pytest.fixture
def location_upload_dir(app, tmp_path):
    upload_dir = tmp_path / 'uploads' / 'products'
    app.config['PRODUCT_IMAGE_UPLOAD_DIR'] = str(upload_dir)
    return upload_dir


def _image_tuple(content=JPEG_BYTES, filename='product.jpg', content_type='image/jpeg'):
    return (BytesIO(content), filename, content_type)


def _new_product_data(title='Location Product', trade_location=None):
    data = {
        'title': title,
        'description': f'{title} description',
        'price': '1234',
    }
    if trade_location is not None:
        data['trade_location'] = trade_location
    return data


def _edit_product_data(title='Edited Location Product', trade_location=None):
    data = {
        'title': title,
        'description': f'{title} description',
        'price': '4321',
        'status': Product.STATUS_SELLING,
    }
    if trade_location is not None:
        data['trade_location'] = trade_location
    return data


def _post_new_product(client, title='Location Product', trade_location=None, image=None):
    data = _new_product_data(title=title, trade_location=trade_location)
    if image is not None:
        data['image'] = image
        return client.post('/product/new', data=data, content_type='multipart/form-data')
    return client.post('/product/new', data=data)


def _post_edit_product(client, product_id, title='Edited Location Product', trade_location=None, image=None):
    data = _edit_product_data(title=title, trade_location=trade_location)
    if image is not None:
        data['image'] = image
        return client.post(
            f'/product/{product_id}/edit',
            data=data,
            content_type='multipart/form-data',
        )
    return client.post(f'/product/{product_id}/edit', data=data)


def _product_by_title(app, title):
    with app.app_context():
        return Product.query.filter_by(title=title).one()


def _product_count(app):
    with app.app_context():
        return Product.query.count()


def _create_product(app, seller_id, title, trade_location=None):
    with app.app_context():
        product = Product(
            seller_id=seller_id,
            title=title,
            description=f'{title} description',
            price=1000,
            trade_location=trade_location,
            status=Product.STATUS_SELLING,
        )
        db.session.add(product)
        db.session.commit()
        return product.id


def _image_path(upload_dir, product_id, extension='jpg'):
    return upload_dir / f'{product_id}.{extension}'


def test_product_can_be_created_without_location(client, app, auth_helper, normal_user, location_upload_dir):
    auth_helper.login('testuser')

    response = _post_new_product(client, title='No Location Product')

    assert response.status_code == 302
    product = _product_by_title(app, 'No Location Product')
    assert product.seller_id == normal_user
    assert product.trade_location is None


def test_product_can_be_created_with_valid_korean_location(client, app, auth_helper, normal_user, location_upload_dir):
    auth_helper.login('testuser')

    response = _post_new_product(client, title='Korean Location Product', trade_location='대구 수성구')

    assert response.status_code == 302
    product = _product_by_title(app, 'Korean Location Product')
    assert product.seller_id == normal_user
    assert product.trade_location == '대구 수성구'


def test_location_whitespace_is_normalized(client, app, auth_helper, normal_user, location_upload_dir):
    auth_helper.login('testuser')

    response = _post_new_product(client, title='Normalized Location Product', trade_location='  서울 강남역  ')

    assert response.status_code == 302
    product = _product_by_title(app, 'Normalized Location Product')
    assert product.trade_location == '서울 강남역'


@pytest.mark.parametrize('bad_location', ['가' * 121, '대구\x00수성구', '서울\n강남역', '판교\t역'])
def test_invalid_location_is_rejected_without_creating_product(
    client,
    app,
    auth_helper,
    normal_user,
    location_upload_dir,
    bad_location,
):
    auth_helper.login('testuser')
    before_count = _product_count(app)

    response = _post_new_product(client, title='Invalid Location Product', trade_location=bad_location)

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/product/new')
    assert _product_count(app) == before_count


def test_invalid_location_with_image_leaves_no_uploaded_image(client, app, auth_helper, normal_user, location_upload_dir):
    auth_helper.login('testuser')
    before_count = _product_count(app)

    response = _post_new_product(
        client,
        title='Invalid Location With Image Product',
        trade_location='서울\n강남역',
        image=_image_tuple(),
    )

    assert response.status_code == 302
    assert _product_count(app) == before_count
    if location_upload_dir.exists():
        assert not [path for path in location_upload_dir.iterdir() if path.is_file()]


def test_owner_can_edit_product_location(client, app, auth_helper, product, location_upload_dir):
    auth_helper.login('testuser')

    response = _post_edit_product(client, product, trade_location='판교역 인근')

    assert response.status_code == 302
    with app.app_context():
        updated = db.session.get(Product, product)
        assert updated.trade_location == '판교역 인근'


def test_non_owner_cannot_edit_another_users_location(
    client,
    app,
    auth_helper,
    normal_user,
    second_user,
    product,
    location_upload_dir,
):
    auth_helper.login('seconduser')

    response = _post_edit_product(client, product, trade_location='서울 강남역')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/dashboard')
    with app.app_context():
        unchanged = db.session.get(Product, product)
        assert unchanged.seller_id == normal_user
        assert unchanged.seller_id != second_user
        assert unchanged.trade_location is None


def test_invalid_edit_preserves_previous_location_and_image(
    client,
    app,
    auth_helper,
    product,
    location_upload_dir,
):
    auth_helper.login('testuser')
    first_response = _post_edit_product(
        client,
        product,
        title='Initial Image Location Product',
        trade_location='대구 수성구',
        image=_image_tuple(),
    )
    assert first_response.status_code == 302
    original_image = _image_path(location_upload_dir, product)
    assert original_image.is_file()
    original_bytes = original_image.read_bytes()

    response = _post_edit_product(
        client,
        product,
        title='Invalid Edit Location Product',
        trade_location='대구\n수성구',
        image=_image_tuple(filename='replacement.jpg'),
    )

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/product/{product}/edit')
    assert original_image.is_file()
    assert original_image.read_bytes() == original_bytes
    with app.app_context():
        unchanged = db.session.get(Product, product)
        assert unchanged.title == 'Initial Image Location Product'
        assert unchanged.trade_location == '대구 수성구'


def test_product_detail_displays_location_and_missing_placeholder(
    client,
    app,
    normal_user,
    location_upload_dir,
):
    with_location = _create_product(app, normal_user, 'Detail Location Product', trade_location='서울 강남역')
    without_location = _create_product(app, normal_user, 'Detail Missing Location Product')

    location_response = client.get(f'/product/{with_location}')
    missing_response = client.get(f'/product/{without_location}')

    assert location_response.status_code == 200
    assert '거래 지역: 서울 강남역' in location_response.get_data(as_text=True)
    assert missing_response.status_code == 200
    assert '거래 지역: 위치 미등록' in missing_response.get_data(as_text=True)


def test_mypage_displays_owned_product_location(client, app, auth_helper, normal_user, location_upload_dir):
    _create_product(app, normal_user, 'My Location Product', trade_location='판교역 인근')
    auth_helper.login('testuser')

    response = client.get('/mypage')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'My Location Product' in body
    assert '판교역 인근' in body


def test_location_filter_finds_matching_and_excludes_nonmatching_products(
    client,
    app,
    auth_helper,
    normal_user,
    location_upload_dir,
):
    _create_product(app, normal_user, 'Suseong Product', trade_location='대구 수성구')
    _create_product(app, normal_user, 'Gangnam Product', trade_location='서울 강남역')
    auth_helper.login('testuser')

    response = client.get('/dashboard?location=수성구')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'Suseong Product' in body
    assert 'Gangnam Product' not in body


def test_general_search_and_location_filter_work_together(
    client,
    app,
    auth_helper,
    normal_user,
    location_upload_dir,
):
    _create_product(app, normal_user, 'Laptop Product', trade_location='서울 강남역')
    _create_product(app, normal_user, 'Laptop Other Location', trade_location='판교역 인근')
    _create_product(app, normal_user, 'Book Product', trade_location='서울 강남역')
    auth_helper.login('testuser')

    response = client.get('/dashboard?q=Laptop&location=강남')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'Laptop Product' in body
    assert 'Laptop Other Location' not in body
    assert 'Book Product' not in body


def test_location_search_does_not_expose_deleted_products(
    client,
    app,
    auth_helper,
    normal_user,
    location_upload_dir,
):
    product_id = _create_product(app, normal_user, 'Deleted Location Product', trade_location='삭제 지역')
    auth_helper.login('testuser')
    delete_response = client.post(f'/product/{product_id}/delete')
    assert delete_response.status_code == 302

    response = client.get('/dashboard?location=삭제')

    assert response.status_code == 200
    assert 'Deleted Location Product' not in response.get_data(as_text=True)


def test_location_search_safely_handles_quote_and_wildcard_like_input(
    client,
    app,
    auth_helper,
    normal_user,
    location_upload_dir,
):
    _create_product(app, normal_user, 'Literal Wildcard Product', trade_location='100% 안전_구역')
    _create_product(app, normal_user, 'Plain Product', trade_location='서울 강남역')
    auth_helper.login('testuser')

    literal_response = client.get('/dashboard?location=100%25+안전_구역')
    wildcard_response = client.get('/dashboard?location=%25')
    quote_response = client.get("/dashboard?location=강남' OR '1'='1")

    assert literal_response.status_code == 200
    assert 'Literal Wildcard Product' in literal_response.get_data(as_text=True)
    assert wildcard_response.status_code == 200
    wildcard_body = wildcard_response.get_data(as_text=True)
    assert 'Literal Wildcard Product' in wildcard_body
    assert 'Plain Product' not in wildcard_body
    assert quote_response.status_code == 200
    quote_body = quote_response.get_data(as_text=True)
    assert 'Literal Wildcard Product' not in quote_body
    assert 'Plain Product' not in quote_body


def test_existing_image_create_edit_delete_and_ownership_behavior_continues(
    client,
    app,
    auth_helper,
    normal_user,
    second_user,
    location_upload_dir,
):
    auth_helper.login('testuser')
    create_response = _post_new_product(
        client,
        title='Image Location Product',
        trade_location='서울 강남역',
        image=_image_tuple(),
    )
    assert create_response.status_code == 302
    product = _product_by_title(app, 'Image Location Product')
    image_path = _image_path(location_upload_dir, product.id)
    assert image_path.is_file()
    assert product.seller_id == normal_user

    auth_helper.login('seconduser')
    denied_response = _post_edit_product(client, product.id, trade_location='판교역 인근')
    assert denied_response.status_code == 302
    assert denied_response.headers['Location'].endswith('/dashboard')

    auth_helper.login('testuser')
    edit_response = _post_edit_product(client, product.id, title='Edited Image Location Product', trade_location='판교역 인근')
    assert edit_response.status_code == 302
    with app.app_context():
        edited = db.session.get(Product, product.id)
        assert edited.title == 'Edited Image Location Product'
        assert edited.trade_location == '판교역 인근'
        assert edited.seller_id != second_user

    delete_response = client.post(f'/product/{product.id}/delete')
    assert delete_response.status_code == 302
    assert not image_path.exists()
    with app.app_context():
        assert db.session.get(Product, product.id) is None


def test_migration_script_adds_column_once_and_preserves_rows(tmp_path):
    db_path = tmp_path / 'migration.sqlite'
    engine = create_engine(f'sqlite:///{db_path}')
    with engine.begin() as connection:
        connection.execute(text(
            'CREATE TABLE product ('
            'id VARCHAR(36) PRIMARY KEY, '
            'title VARCHAR(100) NOT NULL'
            ')'
        ))
        connection.execute(text("INSERT INTO product (id, title) VALUES ('p1', 'Existing Product')"))

    first_result = add_trade_location_column(engine)
    second_result = add_trade_location_column(engine)

    inspector = inspect(engine)
    columns = [column['name'] for column in inspector.get_columns('product')]
    assert first_result == 0
    assert second_result == 0
    assert columns.count('trade_location') == 1
    with engine.connect() as connection:
        rows = connection.execute(text('SELECT id, title FROM product')).all()
    assert [tuple(row) for row in rows] == [('p1', 'Existing Product')]


def test_migration_script_stops_safely_for_non_sqlite_database():
    class FakeDialect:
        name = 'postgresql'

    class FakeEngine:
        dialect = FakeDialect()

    engine = FakeEngine()

    result = add_trade_location_column(engine)

    assert result == 2
