from io import BytesIO
import uuid

import pytest

from market.extensions import db
from market.models import Product


JPEG_BYTES = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00' + b'\x00' * 16
PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'\x00' * 16
WEBP_BYTES = b'RIFF\x10\x00\x00\x00WEBPVP8 ' + b'\x00' * 8


@pytest.fixture
def product_upload_dir(app, tmp_path):
    upload_dir = tmp_path / 'uploads' / 'products'
    app.config['PRODUCT_IMAGE_UPLOAD_DIR'] = str(upload_dir)
    app.config['PRODUCT_IMAGE_MAX_BYTES'] = 5 * 1024 * 1024
    return upload_dir


def _image_tuple(content, filename, content_type='application/octet-stream'):
    return (BytesIO(content), filename, content_type)


def _post_new_product(client, title, image=None):
    data = {
        'title': title,
        'description': f'{title} description',
        'price': '1234',
    }
    if image is not None:
        data['image'] = image
        return client.post('/product/new', data=data, content_type='multipart/form-data')
    return client.post('/product/new', data=data)


def _post_edit_product(client, product_id, title, image=None, status=Product.STATUS_SELLING):
    data = {
        'title': title,
        'description': f'{title} description',
        'price': '4321',
        'status': status,
    }
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


def _image_path(upload_dir, product_id, extension):
    return upload_dir / f'{product_id}.{extension}'


def test_product_can_be_created_without_image(client, app, auth_helper, normal_user, product_upload_dir):
    auth_helper.login('testuser')

    response = _post_new_product(client, 'No Image Product')

    assert response.status_code == 302
    product = _product_by_title(app, 'No Image Product')
    assert product.seller_id == normal_user
    assert product.title == 'No Image Product'
    assert product.description == 'No Image Product description'
    assert product.price == 1234
    assert not product_upload_dir.exists()
    assert client.get(f'/product/{product.id}/image').status_code == 404


@pytest.mark.parametrize(
    ('content', 'filename', 'expected_extension', 'expected_mime'),
    [
        (JPEG_BYTES, 'client-name.jpg', 'jpg', 'image/jpeg'),
        (PNG_BYTES, 'client-name.png', 'png', 'image/png'),
        (WEBP_BYTES, 'client-name.webp', 'webp', 'image/webp'),
    ],
)
def test_authorized_user_can_create_product_with_valid_images(
    client,
    app,
    auth_helper,
    normal_user,
    product_upload_dir,
    content,
    filename,
    expected_extension,
    expected_mime,
):
    auth_helper.login('testuser')

    response = _post_new_product(
        client,
        f'Image Product {expected_extension}',
        _image_tuple(content, filename, 'text/plain'),
    )

    assert response.status_code == 302
    product = _product_by_title(app, f'Image Product {expected_extension}')
    stored_path = _image_path(product_upload_dir, product.id, expected_extension)
    assert stored_path.is_file()
    assert stored_path.read_bytes() == content
    assert product.seller_id == normal_user
    assert not (product_upload_dir / filename).exists()

    image_response = client.get(f'/product/{product.id}/image')
    assert image_response.status_code == 200
    assert image_response.mimetype == expected_mime
    assert image_response.headers['X-Content-Type-Options'] == 'nosniff'
    assert 'inline' in image_response.headers['Content-Disposition']


def test_original_and_path_traversal_filenames_are_not_used(
    client,
    app,
    auth_helper,
    normal_user,
    product_upload_dir,
    tmp_path,
):
    auth_helper.login('testuser')

    response = _post_new_product(
        client,
        'Traversal Filename Product',
        _image_tuple(JPEG_BYTES, '../../evil.jpg', 'image/jpeg'),
    )

    assert response.status_code == 302
    product = _product_by_title(app, 'Traversal Filename Product')
    assert _image_path(product_upload_dir, product.id, 'jpg').is_file()
    assert not (product_upload_dir / 'evil.jpg').exists()
    assert not list(tmp_path.rglob('evil.jpg'))


@pytest.mark.parametrize(
    ('content', 'filename', 'content_type'),
    [
        (JPEG_BYTES, 'image.gif', 'image/gif'),
        (JPEG_BYTES, 'image.png', 'image/png'),
        (b'<html><script>alert(1)</script></html>', 'image.png', 'image/png'),
        (b'', 'empty.jpg', 'image/jpeg'),
    ],
)
def test_invalid_uploads_are_rejected_without_creating_product(
    client,
    app,
    auth_helper,
    normal_user,
    product_upload_dir,
    content,
    filename,
    content_type,
):
    auth_helper.login('testuser')
    before_count = _product_count(app)

    response = _post_new_product(
        client,
        f'Invalid Upload {filename}',
        _image_tuple(content, filename, content_type),
    )

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/product/new')
    assert _product_count(app) == before_count
    if product_upload_dir.exists():
        assert not [path for path in product_upload_dir.iterdir() if path.is_file()]


def test_oversized_upload_returns_safe_response(client, app, auth_helper, normal_user, product_upload_dir):
    auth_helper.login('testuser')
    before_count = _product_count(app)
    oversized = b'\xff\xd8\xff' + b'\x00' * (5 * 1024 * 1024)

    response = _post_new_product(
        client,
        'Oversized Image Product',
        _image_tuple(oversized, 'oversized.jpg', 'image/jpeg'),
    )

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/product/new')
    assert _product_count(app) == before_count
    if product_upload_dir.exists():
        assert not [path for path in product_upload_dir.iterdir() if path.is_file()]


def test_request_larger_than_max_content_length_returns_safe_413(
    client,
    app,
    auth_helper,
    normal_user,
    product_upload_dir,
):
    auth_helper.login('testuser')
    before_count = _product_count(app)
    too_large_for_request = b'\xff\xd8\xff' + b'\x00' * app.config['MAX_CONTENT_LENGTH']

    response = _post_new_product(
        client,
        'Too Large Request Product',
        _image_tuple(too_large_for_request, 'too-large.jpg', 'image/jpeg'),
    )

    assert response.status_code == 413
    body = response.get_data(as_text=True).lower()
    assert 'traceback' not in body
    assert 'werkzeug' not in body
    assert 'requestentitytoolarge' not in body
    assert '/home/' not in body
    assert 'instance/uploads' not in body
    assert _product_count(app) == before_count
    if product_upload_dir.exists():
        assert not [path for path in product_upload_dir.iterdir() if path.is_file()]


def test_nonexistent_product_image_returns_404(client, product_upload_dir):
    response = client.get(f'/product/{uuid.uuid4()}/image')

    assert response.status_code == 404


def test_product_without_image_returns_404(client, product, product_upload_dir):
    response = client.get(f'/product/{product}/image')

    assert response.status_code == 404


def test_unauthorized_user_cannot_replace_another_users_image(
    client,
    app,
    auth_helper,
    normal_user,
    second_user,
    product,
    product_upload_dir,
):
    auth_helper.login('testuser')
    _post_edit_product(client, product, 'Owner Image Product', _image_tuple(JPEG_BYTES, 'owner.jpg'))

    auth_helper.login('seconduser')
    response = _post_edit_product(client, product, 'Attacker Image Product', _image_tuple(PNG_BYTES, 'attacker.png'))

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/dashboard')
    assert _image_path(product_upload_dir, product, 'jpg').is_file()
    assert not _image_path(product_upload_dir, product, 'png').exists()
    with app.app_context():
        unchanged = db.session.get(Product, product)
        assert unchanged.seller_id == normal_user
        assert unchanged.seller_id != second_user
        assert unchanged.title == 'Owner Image Product'


def test_owner_can_replace_image_and_obsolete_old_format_is_removed(
    client,
    app,
    auth_helper,
    product,
    product_upload_dir,
):
    auth_helper.login('testuser')
    _post_edit_product(client, product, 'Initial Image Product', _image_tuple(JPEG_BYTES, 'initial.jpg'))

    response = _post_edit_product(client, product, 'Replacement Image Product', _image_tuple(PNG_BYTES, 'replacement.png'))

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/product/{product}')
    assert not _image_path(product_upload_dir, product, 'jpg').exists()
    assert _image_path(product_upload_dir, product, 'png').is_file()
    with app.app_context():
        updated = db.session.get(Product, product)
        assert updated.title == 'Replacement Image Product'
        assert updated.description == 'Replacement Image Product description'
        assert updated.price == 4321


def test_invalid_replacement_preserves_existing_image_and_product_data(
    client,
    app,
    auth_helper,
    product,
    product_upload_dir,
):
    auth_helper.login('testuser')
    _post_edit_product(client, product, 'Original Image Product', _image_tuple(JPEG_BYTES, 'original.jpg'))
    original_path = _image_path(product_upload_dir, product, 'jpg')
    original_bytes = original_path.read_bytes()

    response = _post_edit_product(client, product, 'Invalid Replacement Product', _image_tuple(JPEG_BYTES, 'bad.png'))

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/product/{product}/edit')
    assert original_path.is_file()
    assert original_path.read_bytes() == original_bytes
    assert not _image_path(product_upload_dir, product, 'png').exists()
    with app.app_context():
        unchanged = db.session.get(Product, product)
        assert unchanged.title == 'Original Image Product'
        assert unchanged.description == 'Original Image Product description'
        assert unchanged.price == 4321


def test_deleting_product_removes_corresponding_image(
    client,
    app,
    auth_helper,
    product,
    product_upload_dir,
):
    auth_helper.login('testuser')
    _post_edit_product(client, product, 'Delete Image Product', _image_tuple(WEBP_BYTES, 'delete.webp'))
    image_path = _image_path(product_upload_dir, product, 'webp')
    assert image_path.is_file()

    response = client.post(f'/product/{product}/delete')

    assert response.status_code == 302
    assert not image_path.exists()
    with app.app_context():
        assert db.session.get(Product, product) is None


def test_deleting_product_with_no_image_still_succeeds(client, app, auth_helper, product, product_upload_dir):
    auth_helper.login('testuser')

    response = client.post(f'/product/{product}/delete')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Product, product) is None


def test_deleting_product_does_not_remove_another_products_image(
    client,
    app,
    auth_helper,
    normal_user,
    product_upload_dir,
):
    auth_helper.login('testuser')
    _post_new_product(client, 'First Product Image', _image_tuple(JPEG_BYTES, 'first.jpg'))
    _post_new_product(client, 'Second Product Image', _image_tuple(PNG_BYTES, 'second.png'))
    first = _product_by_title(app, 'First Product Image')
    second = _product_by_title(app, 'Second Product Image')
    second_image_path = _image_path(product_upload_dir, second.id, 'png')

    response = client.post(f'/product/{first.id}/delete')

    assert response.status_code == 302
    assert not _image_path(product_upload_dir, first.id, 'jpg').exists()
    assert second_image_path.is_file()
    with app.app_context():
        assert db.session.get(Product, second.id) is not None


def test_existing_product_fields_and_ownership_remain_intact_without_image_replacement(
    client,
    app,
    auth_helper,
    normal_user,
    product,
    product_upload_dir,
):
    auth_helper.login('testuser')

    response = _post_edit_product(client, product, 'No Replacement Edit Product')

    assert response.status_code == 302
    with app.app_context():
        updated = db.session.get(Product, product)
        assert updated.seller_id == normal_user
        assert updated.title == 'No Replacement Edit Product'
        assert updated.description == 'No Replacement Edit Product description'
        assert updated.price == 4321
        assert updated.status == Product.STATUS_SELLING
    assert not product_upload_dir.exists()
