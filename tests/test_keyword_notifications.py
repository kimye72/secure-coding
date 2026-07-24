from io import BytesIO
import re

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from market import create_app
from market.extensions import db
from market.keyword_notifications import MAX_NORMALIZED_KEYWORD_LENGTH
from market.models import KeywordSubscription, Notification, Product, Trade, User
from scripts.add_keyword_notifications import (
    migrate_keyword_notifications_schema,
    notification_tables_are_compatible,
)


TEST_PASSWORD = 'TestPassword123!'
JPEG_BYTES = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00' + b'\x00' * 16


@pytest.fixture
def notification_upload_dir(app, tmp_path):
    upload_dir = tmp_path / 'uploads' / 'products'
    app.config['PRODUCT_IMAGE_UPLOAD_DIR'] = str(upload_dir)
    app.config['PRODUCT_IMAGE_MAX_BYTES'] = 5 * 1024 * 1024
    return upload_dir


@pytest.fixture
def csrf_app(tmp_path):
    app = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': True,
        'RATELIMIT_ENABLED': False,
        'SECRET_KEY': 'csrf-keyword-secret',
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


def _image_tuple(content=JPEG_BYTES, filename='image.jpg', content_type='image/jpeg'):
    return (BytesIO(content), filename, content_type)


def _post_product(client, title, description=None, image=None, trade_location='서울 강남역'):
    data = {
        'title': title,
        'description': description if description is not None else f'{title} description',
        'price': '1234',
        'trade_location': trade_location,
    }
    if image is not None:
        data['image'] = image
        return client.post('/product/new', data=data, content_type='multipart/form-data')
    return client.post('/product/new', data=data)


def _post_edit_product(client, product_id, title, description=None):
    return client.post(f'/product/{product_id}/edit', data={
        'title': title,
        'description': description if description is not None else f'{title} description',
        'price': '4321',
        'trade_location': '서울 강남역',
        'status': Product.STATUS_SELLING,
    })


def _product_by_title(title):
    return Product.query.filter_by(title=title).one()


def _subscription_count(user_id):
    return KeywordSubscription.query.filter_by(user_id=user_id).count()


def _notification_count(user_id=None):
    query = Notification.query
    if user_id is not None:
        query = query.filter_by(user_id=user_id)
    return query.count()


def _create_subscription(user_id, keyword, normalized_keyword=None):
    subscription = KeywordSubscription(
        user_id=user_id,
        keyword=keyword,
        normalized_keyword=normalized_keyword or keyword.casefold(),
    )
    db.session.add(subscription)
    db.session.commit()
    return subscription.id


def _create_product_direct(seller_id, title='Direct Product'):
    product = Product(
        seller_id=seller_id,
        title=title,
        description='Direct description',
        price=1000,
        status=Product.STATUS_SELLING,
    )
    db.session.add(product)
    db.session.commit()
    return product.id


def _create_notification(user_id, product_id, keyword='키워드', is_read=False):
    notification = Notification(
        user_id=user_id,
        product_id=product_id,
        matched_keyword=keyword,
        is_read=is_read,
    )
    db.session.add(notification)
    db.session.commit()
    return notification.id


def test_unauthenticated_user_cannot_access_notification_management(client):
    response = client.get('/notifications')

    assert response.status_code == 302
    assert '/login' in response.headers['Location']


def test_active_authenticated_user_can_access_notifications(client, auth_helper, normal_user):
    auth_helper.login('testuser')

    response = client.get('/notifications')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '관심 키워드 등록' in body
    assert '아직 받은 알림이 없습니다.' in body


def test_inactive_user_cannot_access_notifications(client, inactive_user):
    _login_session(client, inactive_user)

    response = client.get('/notifications')

    assert response.status_code == 302
    assert '/login' in response.headers['Location']


def test_valid_korean_keyword_can_be_registered(client, app, auth_helper, normal_user):
    auth_helper.login('testuser')

    response = client.post('/keywords', data={'keyword': '대구 수성구'})

    assert response.status_code == 302
    with app.app_context():
        subscription = KeywordSubscription.query.one()
        assert subscription.user_id == normal_user
        assert subscription.keyword == '대구 수성구'
        assert subscription.normalized_keyword == '대구 수성구'


def test_keyword_whitespace_is_normalized(client, app, auth_helper, normal_user):
    auth_helper.login('testuser')

    response = client.post('/keywords', data={'keyword': '  서울   강남역  '})

    assert response.status_code == 302
    with app.app_context():
        subscription = KeywordSubscription.query.one()
        assert subscription.keyword == '서울 강남역'
        assert subscription.normalized_keyword == '서울 강남역'


def test_keyword_case_variations_are_duplicates(client, app, auth_helper, normal_user):
    auth_helper.login('testuser')

    first = client.post('/keywords', data={'keyword': 'Laptop'})
    second = client.post('/keywords', data={'keyword': '  laptop  '})

    assert first.status_code == 302
    assert second.status_code == 302
    with app.app_context():
        assert _subscription_count(normal_user) == 1
        assert KeywordSubscription.query.one().normalized_keyword == 'laptop'


@pytest.mark.parametrize(
    'keyword',
    [
        '',
        '   ',
        '가' * 81,
        'bad\x00value',
        'bad\nvalue',
        'bad\tvalue',
        'bad\uE000value',
        'bad\u2028value',
        'bad\u2029value',
    ],
)
def test_invalid_keywords_are_rejected(client, app, auth_helper, normal_user, keyword):
    auth_helper.login('testuser')

    response = client.post('/keywords', data={'keyword': keyword})

    assert response.status_code == 302
    with app.app_context():
        assert _subscription_count(normal_user) == 0


def test_unicode_space_separators_are_normalized_to_ascii_space(client, app, auth_helper, normal_user):
    auth_helper.login('testuser')

    response = client.post('/keywords', data={'keyword': '\u2003서울\u00A0\u2002강남역\u2003'})

    assert response.status_code == 302
    with app.app_context():
        subscription = KeywordSubscription.query.one()
        assert subscription.keyword == '서울 강남역'
        assert subscription.normalized_keyword == '서울 강남역'


def test_casefold_expansion_is_stored_completely_and_used_for_duplicates(client, app, auth_helper, normal_user):
    keyword = '\u0130' * 80
    expected_normalized = 'i\u0307' * 80
    assert len(keyword) == 80
    assert len(expected_normalized) == 160
    assert len(expected_normalized) <= MAX_NORMALIZED_KEYWORD_LENGTH
    auth_helper.login('testuser')

    first = client.post('/keywords', data={'keyword': keyword})
    duplicate = client.post('/keywords', data={'keyword': f'  {keyword}  '})

    assert first.status_code == 302
    assert duplicate.status_code == 302
    with app.app_context():
        subscription = KeywordSubscription.query.one()
        assert subscription.keyword == keyword
        assert subscription.normalized_keyword == expected_normalized
        assert len(subscription.normalized_keyword) == 160
        assert _subscription_count(normal_user) == 1


def test_twenty_keyword_limit_is_enforced_and_duplicate_does_not_consume_slot(client, app, auth_helper, normal_user):
    auth_helper.login('testuser')
    with app.app_context():
        for index in range(20):
            _create_subscription(normal_user, f'키워드{index}', f'키워드{index}')

    duplicate = client.post('/keywords', data={'keyword': '키워드1'})
    overflow = client.post('/keywords', data={'keyword': '새키워드'})

    assert duplicate.status_code == 302
    assert overflow.status_code == 302
    with app.app_context():
        assert _subscription_count(normal_user) == 20
        assert KeywordSubscription.query.filter_by(keyword='새키워드').count() == 0


def test_user_cannot_delete_another_users_subscription(client, app, auth_helper, normal_user, second_user):
    with app.app_context():
        other_subscription_id = _create_subscription(second_user, '상대 키워드', '상대 키워드')
    auth_helper.login('testuser')

    response = client.post(f'/keywords/{other_subscription_id}/delete')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(KeywordSubscription, other_subscription_id) is not None


def test_administrator_cannot_delete_another_users_subscription(client, app, auth_helper, normal_user, admin_user):
    with app.app_context():
        subscription_id = _create_subscription(normal_user, '관리자 우회 금지', '관리자 우회 금지')
    auth_helper.login('adminuser')

    response = client.post(f'/keywords/{subscription_id}/delete')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(KeywordSubscription, subscription_id) is not None


def test_subscription_deletion_is_post_only(client, app, normal_user):
    with app.app_context():
        subscription_id = _create_subscription(normal_user, '삭제 키워드', '삭제 키워드')

    response = client.get(f'/keywords/{subscription_id}/delete')

    assert response.status_code == 405


def test_csrf_protects_keyword_creation_and_deletion(csrf_app, csrf_client):
    with csrf_app.app_context():
        user_id = _create_user('csrfkeyword')
        subscription_id = _create_subscription(user_id, 'csrf 키워드', 'csrf 키워드')
    _login_session(csrf_client, user_id)

    create_without_token = csrf_client.post('/keywords', data={'keyword': '새 키워드'})
    delete_without_token = csrf_client.post(f'/keywords/{subscription_id}/delete')
    token = _csrf_token(csrf_client.get('/notifications'))
    create_with_token = csrf_client.post('/keywords', data={'keyword': '새 키워드', 'csrf_token': token})

    assert create_without_token.status_code == 400
    assert delete_without_token.status_code == 400
    assert create_with_token.status_code == 302


def test_matching_product_title_creates_notification(client, app, auth_helper, normal_user, second_user, notification_upload_dir):
    with app.app_context():
        _create_subscription(second_user, '자전거', '자전거')
    auth_helper.login('testuser')

    response = _post_product(client, '튼튼한 자전거')

    assert response.status_code == 302
    with app.app_context():
        notification = Notification.query.one()
        assert notification.user_id == second_user
        assert notification.product.title == '튼튼한 자전거'
        assert notification.matched_keyword == '자전거'


def test_matching_product_description_creates_notification(client, app, auth_helper, normal_user, second_user, notification_upload_dir):
    with app.app_context():
        _create_subscription(second_user, '판교역', '판교역')
    auth_helper.login('testuser')

    response = _post_product(client, '상품 제목', description='판교역 근처에서 거래합니다')

    assert response.status_code == 302
    with app.app_context():
        assert _notification_count(second_user) == 1


def test_nonmatching_product_creates_no_notification(client, app, auth_helper, normal_user, second_user, notification_upload_dir):
    with app.app_context():
        _create_subscription(second_user, '노트북', '노트북')
    auth_helper.login('testuser')

    response = _post_product(client, '책상', description='의자와 함께 판매')

    assert response.status_code == 302
    with app.app_context():
        assert _notification_count(second_user) == 0


def test_seller_and_inactive_subscriber_do_not_receive_notifications(
    client,
    app,
    auth_helper,
    normal_user,
    inactive_user,
    notification_upload_dir,
):
    with app.app_context():
        _create_subscription(normal_user, '카메라', '카메라')
        _create_subscription(inactive_user, '카메라', '카메라')
    auth_helper.login('testuser')

    response = _post_product(client, '카메라 판매')

    assert response.status_code == 302
    with app.app_context():
        assert Notification.query.count() == 0


def test_multiple_matching_keywords_for_one_user_create_one_deterministic_notification(
    client,
    app,
    auth_helper,
    normal_user,
    second_user,
    notification_upload_dir,
):
    with app.app_context():
        _create_subscription(second_user, '노트북', '노트북')
        _create_subscription(second_user, '게이밍', '게이밍')
    auth_helper.login('testuser')

    response = _post_product(client, '게이밍 노트북')

    assert response.status_code == 302
    with app.app_context():
        notification = Notification.query.one()
        assert notification.user_id == second_user
        assert notification.matched_keyword == '노트북'


def test_different_users_can_receive_notification_for_same_product(
    client,
    app,
    auth_helper,
    normal_user,
    second_user,
    admin_user,
    notification_upload_dir,
):
    with app.app_context():
        _create_subscription(second_user, '모니터', '모니터')
        _create_subscription(admin_user, '모니터', '모니터')
    auth_helper.login('testuser')

    response = _post_product(client, '모니터 판매')

    assert response.status_code == 302
    with app.app_context():
        assert Notification.query.count() == 2
        assert {notification.user_id for notification in Notification.query.all()} == {second_user, admin_user}


def test_literal_keyword_characters_are_matched_without_sql_wildcards(client, app, auth_helper, normal_user, second_user, notification_upload_dir):
    with app.app_context():
        _create_subscription(second_user, "100%_\\'특가", "100%_\\'특가")
    auth_helper.login('testuser')

    matched = _post_product(client, "오늘 100%_\\'특가 상품")
    unmatched = _post_product(client, "오늘 100ABC특가 상품")

    assert matched.status_code == 302
    assert unmatched.status_code == 302
    with app.app_context():
        assert Notification.query.count() == 1


def test_failed_product_creation_creates_no_notification(client, app, auth_helper, normal_user, second_user, notification_upload_dir):
    with app.app_context():
        _create_subscription(second_user, '실패', '실패')
    auth_helper.login('testuser')

    response = client.post('/product/new', data={'title': '', 'description': '실패 설명', 'price': '1000'})

    assert response.status_code == 302
    with app.app_context():
        assert Product.query.filter(Product.description == '실패 설명').count() == 0
        assert Notification.query.count() == 0


def test_invalid_image_creates_no_notification_or_product(client, app, auth_helper, normal_user, second_user, notification_upload_dir):
    with app.app_context():
        _create_subscription(second_user, '이미지실패', '이미지실패')
    auth_helper.login('testuser')

    response = _post_product(client, '이미지실패 상품', image=_image_tuple(b'<html></html>', 'bad.jpg', 'image/jpeg'))

    assert response.status_code == 302
    with app.app_context():
        assert Product.query.filter_by(title='이미지실패 상품').count() == 0
        assert Notification.query.count() == 0
    if notification_upload_dir.exists():
        assert not [path for path in notification_upload_dir.iterdir() if path.is_file()]


def test_invalid_trade_location_creates_no_notification_or_image(client, app, auth_helper, normal_user, second_user, notification_upload_dir):
    with app.app_context():
        _create_subscription(second_user, '지역실패', '지역실패')
    auth_helper.login('testuser')

    response = _post_product(
        client,
        '지역실패 상품',
        image=_image_tuple(),
        trade_location='bad\nlocation',
    )

    assert response.status_code == 302
    with app.app_context():
        assert Product.query.filter_by(title='지역실패 상품').count() == 0
        assert Notification.query.count() == 0
    assert not notification_upload_dir.exists()


def test_successful_image_upload_and_notification_commit_together(
    client,
    app,
    auth_helper,
    normal_user,
    second_user,
    notification_upload_dir,
):
    with app.app_context():
        _create_subscription(second_user, '이미지성공', '이미지성공')
    auth_helper.login('testuser')

    response = _post_product(client, '이미지성공 상품', image=_image_tuple())

    assert response.status_code == 302
    with app.app_context():
        product = _product_by_title('이미지성공 상품')
        notification = Notification.query.one()
        assert notification.product_id == product.id
        assert (notification_upload_dir / f'{product.id}.jpg').is_file()


def test_product_edit_does_not_create_new_notification(client, app, auth_helper, second_user, product, notification_upload_dir):
    with app.app_context():
        _create_subscription(second_user, '수정매칭', '수정매칭')
    auth_helper.login('testuser')

    response = _post_edit_product(client, product, '수정매칭 제목')

    assert response.status_code == 302
    with app.app_context():
        assert Notification.query.count() == 0


def test_existing_products_do_not_generate_retroactive_notifications(client, app, auth_helper, normal_user):
    with app.app_context():
        _create_product_direct(normal_user, '기존 노트북')
    auth_helper.login('seconduser')

    response = client.post('/keywords', data={'keyword': '노트북'})

    assert response.status_code == 302
    with app.app_context():
        assert Notification.query.count() == 0


def test_user_sees_only_own_notifications_and_not_other_subscriptions(client, app, auth_helper, normal_user, second_user):
    with app.app_context():
        own_product = _create_product_direct(second_user, '내 알림 상품')
        other_product = _create_product_direct(second_user, '다른 알림 상품')
        _create_subscription(second_user, '상대비밀', '상대비밀')
        _create_notification(normal_user, own_product, '내키워드')
        _create_notification(second_user, other_product, '상대키워드')
    auth_helper.login('testuser')

    response = client.get('/notifications')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '내 알림 상품' in body
    assert '내키워드' in body
    assert '다른 알림 상품' not in body
    assert '상대비밀' not in body
    assert f'/product/{own_product}' in body


def test_rendered_product_title_and_keyword_are_escaped(client, app, auth_helper, normal_user, second_user):
    with app.app_context():
        product_id = _create_product_direct(second_user, '<script>상품</script>')
        _create_notification(normal_user, product_id, '<b>키워드</b>')
    auth_helper.login('testuser')

    body = client.get('/notifications').get_data(as_text=True)

    assert '&lt;script&gt;상품&lt;/script&gt;' in body
    assert '&lt;b&gt;키워드&lt;/b&gt;' in body
    assert '<script>상품</script>' not in body
    assert '<b>키워드</b>' not in body


def test_user_can_mark_notification_read_and_repeated_read_is_idempotent(client, app, auth_helper, normal_user, second_user):
    with app.app_context():
        product_id = _create_product_direct(second_user, '읽음 상품')
        notification_id = _create_notification(normal_user, product_id)
    auth_helper.login('testuser')

    first = client.post(f'/notifications/{notification_id}/read')
    second = client.post(f'/notifications/{notification_id}/read')

    assert first.status_code == 302
    assert second.status_code == 302
    with app.app_context():
        assert db.session.get(Notification, notification_id).is_read is True


def test_user_cannot_mark_another_users_notification_read(client, app, auth_helper, normal_user, second_user):
    with app.app_context():
        product_id = _create_product_direct(normal_user, '상대 알림 상품')
        notification_id = _create_notification(second_user, product_id)
    auth_helper.login('testuser')

    response = client.post(f'/notifications/{notification_id}/read')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Notification, notification_id).is_read is False


def test_administrator_cannot_mark_another_users_notification_read(client, app, auth_helper, normal_user, second_user, admin_user):
    with app.app_context():
        product_id = _create_product_direct(second_user, '관리자 읽음 우회 금지')
        notification_id = _create_notification(normal_user, product_id)
    auth_helper.login('adminuser')

    response = client.post(f'/notifications/{notification_id}/read')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Notification, notification_id).is_read is False


def test_get_read_route_returns_405(client, app, normal_user, product):
    with app.app_context():
        notification_id = _create_notification(normal_user, product)

    response = client.get(f'/notifications/{notification_id}/read')

    assert response.status_code == 405


def test_read_all_marks_only_current_users_notifications(client, app, auth_helper, normal_user, second_user):
    with app.app_context():
        product_id = _create_product_direct(second_user, '전체읽음 상품')
        own_notification = _create_notification(normal_user, product_id)
        other_notification = _create_notification(second_user, product_id)
    auth_helper.login('testuser')

    response = client.post('/notifications/read-all')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Notification, own_notification).is_read is True
        assert db.session.get(Notification, other_notification).is_read is False


def test_csrf_protects_notification_read_routes(csrf_app, csrf_client):
    with csrf_app.app_context():
        user_id = _create_user('csrfreader')
        seller_id = _create_user('csrfseller')
        product_id = _create_product_direct(seller_id, 'CSRF Read Product')
        notification_id = _create_notification(user_id, product_id)
    _login_session(csrf_client, user_id)

    single = csrf_client.post(f'/notifications/{notification_id}/read')
    all_response = csrf_client.post('/notifications/read-all')

    assert single.status_code == 400
    assert all_response.status_code == 400


def test_notifications_page_does_not_render_sensitive_fields(client, app, auth_helper, normal_user, second_user):
    with app.app_context():
        product_id = _create_product_direct(second_user, '민감정보 점검')
        _create_subscription(normal_user, '민감키워드', '민감키워드')
        _create_notification(normal_user, product_id, '민감키워드')
    auth_helper.login('testuser')

    body = client.get('/notifications').get_data(as_text=True)

    for sensitive in ('password_hash', TEST_PASSWORD, 'csrf-keyword-secret', 'role', 'BAN', 'session', '신고 내용'):
        assert sensitive not in body


def test_deleting_product_removes_only_notifications_for_that_product(client, app, auth_helper, normal_user, second_user):
    with app.app_context():
        deleted_product = _create_product_direct(normal_user, '삭제 알림 상품')
        kept_product = _create_product_direct(normal_user, '유지 알림 상품')
        deleted_notification = _create_notification(second_user, deleted_product, '삭제')
        kept_notification = _create_notification(second_user, kept_product, '유지')
    auth_helper.login('testuser')

    response = client.post(f'/product/{deleted_product}/delete')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Product, deleted_product) is None
        assert db.session.get(Notification, deleted_notification) is None
        assert db.session.get(Notification, kept_notification) is not None


def test_unauthorized_product_deletion_remains_denied_and_notifications_remain(
    client,
    app,
    auth_helper,
    normal_user,
    second_user,
):
    with app.app_context():
        product_id = _create_product_direct(second_user, '권한 없는 삭제')
        notification_id = _create_notification(normal_user, product_id)
    auth_helper.login('testuser')

    response = client.post(f'/product/{product_id}/delete')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Product, product_id) is not None
        assert db.session.get(Notification, notification_id) is not None


def test_failed_product_deletion_preserves_product_and_notifications(
    client,
    app,
    auth_helper,
    normal_user,
    second_user,
    monkeypatch,
):
    with app.app_context():
        product_id = _create_product_direct(normal_user, '실패 삭제 상품')
        notification_id = _create_notification(second_user, product_id)
    auth_helper.login('testuser')

    def fail_commit():
        raise SQLAlchemyError()

    with monkeypatch.context() as patch:
        patch.setattr(db.session, 'commit', fail_commit)
        response = client.post(f'/product/{product_id}/delete')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Product, product_id) is not None
        assert db.session.get(Notification, notification_id) is not None


def test_product_with_trade_still_cannot_be_deleted_and_notifications_remain(client, app, auth_helper, normal_user, second_user):
    with app.app_context():
        product_id = _create_product_direct(normal_user, '거래 있음 상품')
        trade = Trade(product_id=product_id, buyer_id=second_user, seller_id=normal_user, status=Trade.STATUS_PENDING)
        db.session.add(trade)
        notification_id = _create_notification(second_user, product_id)
    auth_helper.login('testuser')

    response = client.post(f'/product/{product_id}/delete')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Product, product_id) is not None
        assert db.session.get(Notification, notification_id) is not None


def _migration_base_tables(engine):
    User.__table__.create(bind=engine)
    Product.__table__.create(bind=engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO user (id, username, password_hash, bio, role, is_active, created_at) "
            "VALUES ('user1', 'user1', 'hash', NULL, 'USER', 1, '2026-01-01 00:00:00')"
        ))
        connection.execute(text(
            "INSERT INTO product "
            "(id, seller_id, title, description, price, trade_location, status, created_at, updated_at) "
            "VALUES ('product1', 'user1', 'Product', 'Desc', 1000, NULL, 'SELLING', "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        ))


def _migration_rows(engine, table_name):
    with engine.connect() as connection:
        rows = connection.execute(text(f'SELECT * FROM {table_name} ORDER BY id')).all()
    return [tuple(row) for row in rows]


def _create_malformed_keyword_subscription_table(engine, ddl_fragment):
    with engine.begin() as connection:
        connection.execute(text(f'CREATE TABLE keyword_subscription ({ddl_fragment})'))
        connection.execute(text(
            "INSERT INTO keyword_subscription (id, user_id, keyword, normalized_keyword, created_at) "
            "VALUES ('badsub', 'user1', '보존', '보존', '2026-01-02 00:00:00')"
        ))


def _compatible_keyword_subscription_ddl(normalized_length=MAX_NORMALIZED_KEYWORD_LENGTH):
    return (
        'id VARCHAR(36) NOT NULL, '
        'user_id VARCHAR(36) NOT NULL, '
        'keyword VARCHAR(80) NOT NULL, '
        f'normalized_keyword VARCHAR({normalized_length}) NOT NULL, '
        'created_at DATETIME NOT NULL, '
        'PRIMARY KEY (id), '
        'CONSTRAINT uq_keyword_subscription_user_normalized UNIQUE (user_id, normalized_keyword), '
        'FOREIGN KEY(user_id) REFERENCES "user" (id)'
    )


def _create_incompatible_keyword_table_and_assert_safe_stop(tmp_path, ddl_fragment):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_keyword_partial.sqlite'}")
    _migration_base_tables(engine)
    _create_malformed_keyword_subscription_table(engine, ddl_fragment)
    before_rows = _migration_rows(engine, 'keyword_subscription')

    result = migrate_keyword_notifications_schema(engine)

    assert result == 2
    table_names = inspect(engine).get_table_names()
    assert 'keyword_subscription' in table_names
    assert 'notification' not in table_names
    assert _migration_rows(engine, 'keyword_subscription') == before_rows
    return engine


def test_keyword_notification_migration_creates_tables_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'notifications.sqlite'}")
    _migration_base_tables(engine)

    first = migrate_keyword_notifications_schema(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO keyword_subscription (id, user_id, keyword, normalized_keyword, created_at) "
            "VALUES ('sub1', 'user1', '노트북', '노트북', '2026-01-02 00:00:00')"
        ))
        connection.execute(text(
            "INSERT INTO notification (id, user_id, product_id, matched_keyword, is_read, created_at) "
            "VALUES ('note1', 'user1', 'product1', '노트북', 0, '2026-01-03 00:00:00')"
        ))
    subscription_rows = _migration_rows(engine, 'keyword_subscription')
    notification_rows = _migration_rows(engine, 'notification')
    second = migrate_keyword_notifications_schema(engine)

    assert first == 0
    assert second == 0
    assert notification_tables_are_compatible(engine) is True
    assert _migration_rows(engine, 'keyword_subscription') == subscription_rows
    assert _migration_rows(engine, 'notification') == notification_rows
    inspector = inspect(engine)
    keyword_uniques = [constraint['column_names'] for constraint in inspector.get_unique_constraints('keyword_subscription')]
    notification_uniques = [constraint['column_names'] for constraint in inspector.get_unique_constraints('notification')]
    assert ['user_id', 'normalized_keyword'] in keyword_uniques
    assert ['user_id', 'product_id'] in notification_uniques
    with engine.connect() as connection:
        assert connection.execute(text('PRAGMA foreign_key_check')).all() == []


def test_keyword_notification_migration_accepts_exact_current_model_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'exact_model_schema.sqlite'}")
    _migration_base_tables(engine)
    KeywordSubscription.__table__.create(bind=engine)
    Notification.__table__.create(bind=engine)

    assert notification_tables_are_compatible(engine) is True
    assert migrate_keyword_notifications_schema(engine) == 0


def test_keyword_notification_migration_completes_partial_compatible_state(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'partial_compatible.sqlite'}")
    _migration_base_tables(engine)
    KeywordSubscription.__table__.create(bind=engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO keyword_subscription (id, user_id, keyword, normalized_keyword, created_at) "
            "VALUES ('sub1', 'user1', '책상', '책상', '2026-01-02 00:00:00')"
        ))

    result = migrate_keyword_notifications_schema(engine)

    assert result == 0
    table_names = inspect(engine).get_table_names()
    assert 'keyword_subscription' in table_names
    assert 'notification' in table_names
    assert _migration_rows(engine, 'keyword_subscription')[0][0] == 'sub1'


def test_keyword_notification_migration_completes_notification_only_compatible_state(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'notification_only.sqlite'}")
    _migration_base_tables(engine)
    Notification.__table__.create(bind=engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO notification (id, user_id, product_id, matched_keyword, is_read, created_at) "
            "VALUES ('note1', 'user1', 'product1', '책상', 0, '2026-01-03 00:00:00')"
        ))

    result = migrate_keyword_notifications_schema(engine)

    assert result == 0
    table_names = inspect(engine).get_table_names()
    assert 'keyword_subscription' in table_names
    assert 'notification' in table_names
    assert _migration_rows(engine, 'notification')[0][0] == 'note1'


def test_keyword_notification_migration_rejects_same_named_table_without_primary_key(tmp_path):
    _create_incompatible_keyword_table_and_assert_safe_stop(
        tmp_path,
        'id VARCHAR(36) NOT NULL, '
        'user_id VARCHAR(36) NOT NULL, '
        'keyword VARCHAR(80) NOT NULL, '
        f'normalized_keyword VARCHAR({MAX_NORMALIZED_KEYWORD_LENGTH}) NOT NULL, '
        'created_at DATETIME NOT NULL, '
        'CONSTRAINT uq_keyword_subscription_user_normalized UNIQUE (user_id, normalized_keyword), '
        'FOREIGN KEY(user_id) REFERENCES "user" (id)',
    )


def test_keyword_notification_migration_rejects_nullable_required_column(tmp_path):
    _create_incompatible_keyword_table_and_assert_safe_stop(
        tmp_path,
        'id VARCHAR(36) NOT NULL, '
        'user_id VARCHAR(36) NOT NULL, '
        'keyword VARCHAR(80) NULL, '
        f'normalized_keyword VARCHAR({MAX_NORMALIZED_KEYWORD_LENGTH}) NOT NULL, '
        'created_at DATETIME NOT NULL, '
        'PRIMARY KEY (id), '
        'CONSTRAINT uq_keyword_subscription_user_normalized UNIQUE (user_id, normalized_keyword), '
        'FOREIGN KEY(user_id) REFERENCES "user" (id)',
    )


def test_keyword_notification_migration_rejects_incorrect_normalized_keyword_length(tmp_path):
    engine = _create_incompatible_keyword_table_and_assert_safe_stop(
        tmp_path,
        _compatible_keyword_subscription_ddl(normalized_length=80),
    )
    columns = {column['name']: column for column in inspect(engine).get_columns('keyword_subscription')}
    assert getattr(columns['normalized_keyword']['type'], 'length', None) == 80


def test_keyword_notification_migration_rejects_missing_lookup_index(tmp_path):
    _create_incompatible_keyword_table_and_assert_safe_stop(
        tmp_path,
        _compatible_keyword_subscription_ddl(),
    )


def test_keyword_notification_migration_rejects_malformed_notification_table_without_creating_keyword_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_notification_partial.sqlite'}")
    _migration_base_tables(engine)
    with engine.begin() as connection:
        connection.execute(text(
            'CREATE TABLE notification ('
            'id VARCHAR(36) NOT NULL, '
            'user_id VARCHAR(36) NOT NULL, '
            'product_id VARCHAR(36) NOT NULL, '
            'matched_keyword VARCHAR(80) NOT NULL, '
            'is_read INTEGER NOT NULL, '
            'created_at DATETIME NOT NULL, '
            'PRIMARY KEY (id), '
            'CONSTRAINT uq_notification_user_product UNIQUE (user_id, product_id), '
            'FOREIGN KEY(user_id) REFERENCES "user" (id), '
            'FOREIGN KEY(product_id) REFERENCES product (id)'
            ')'
        ))
        connection.execute(text(
            "INSERT INTO notification (id, user_id, product_id, matched_keyword, is_read, created_at) "
            "VALUES ('badnote', 'user1', 'product1', '보존', 0, '2026-01-03 00:00:00')"
        ))
    before_rows = _migration_rows(engine, 'notification')

    result = migrate_keyword_notifications_schema(engine)

    assert result == 2
    table_names = inspect(engine).get_table_names()
    assert 'keyword_subscription' not in table_names
    assert 'notification' in table_names
    assert _migration_rows(engine, 'notification') == before_rows


def test_keyword_notification_migration_stops_for_partial_incompatible_state(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'partial_incompatible.sqlite'}")
    _migration_base_tables(engine)
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE keyword_subscription (id VARCHAR(36) PRIMARY KEY)'))
        connection.execute(text("INSERT INTO keyword_subscription (id) VALUES ('preserve')"))

    result = migrate_keyword_notifications_schema(engine)

    assert result == 2
    table_names = inspect(engine).get_table_names()
    assert 'keyword_subscription' in table_names
    assert 'notification' not in table_names
    with engine.connect() as connection:
        rows = connection.execute(text('SELECT id FROM keyword_subscription')).all()
    assert [tuple(row) for row in rows] == [('preserve',)]


def test_keyword_notification_migration_stops_for_unsupported_database():
    class FakeDialect:
        name = 'postgresql'

    class FakeEngine:
        dialect = FakeDialect()

    assert migrate_keyword_notifications_schema(FakeEngine()) == 2
    assert notification_tables_are_compatible(FakeEngine()) is False
