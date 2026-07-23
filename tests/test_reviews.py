from html import escape

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from market import create_app
from market.extensions import db
from market.models import Product, Review, Trade, User
from scripts.add_reviews_table import (
    create_reviews_table,
    migrate_reviews_schema,
    trade_status_supports_completed,
)


TEST_PASSWORD = 'TestPassword123!'


def _create_trade(app, buyer_id, seller_id, product_id, status=Trade.STATUS_PENDING):
    with app.app_context():
        trade = Trade(
            product_id=product_id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            status=status,
        )
        product = db.session.get(Product, product_id)
        if status == Trade.STATUS_ACCEPTED and product is not None:
            product.status = Product.STATUS_RESERVED
        db.session.add(trade)
        db.session.commit()
        return trade.id


def _create_user(username, role=User.ROLE_USER, is_active=True):
    user = User(username=username, role=role, is_active=is_active)
    user.set_password(TEST_PASSWORD)
    db.session.add(user)
    db.session.commit()
    return user.id


def _complete_trade(app, trade_id):
    with app.app_context():
        trade = db.session.get(Trade, trade_id)
        product = db.session.get(Product, trade.product_id)
        trade.status = Trade.STATUS_COMPLETED
        product.status = Product.STATUS_SOLD
        db.session.commit()


def _review_count(app):
    with app.app_context():
        return Review.query.count()


def _post_review(client, trade_id, rating='5', content='좋은 거래였습니다.', extra=None):
    data = {'rating': rating, 'content': content}
    if extra:
        data.update(extra)
    return client.post(f'/trade/{trade_id}/review', data=data)


@pytest.fixture
def accepted_trade(app, normal_user, second_user, product):
    return _create_trade(app, buyer_id=second_user, seller_id=normal_user, product_id=product, status=Trade.STATUS_ACCEPTED)


@pytest.fixture
def completed_trade(app, accepted_trade):
    _complete_trade(app, accepted_trade)
    return accepted_trade


@pytest.fixture
def csrf_app():
    app = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': True,
        'RATELIMIT_ENABLED': False,
        'SECRET_KEY': 'csrf-review-secret',
    })
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def csrf_client(csrf_app):
    return csrf_app.test_client()


def test_unauthenticated_user_cannot_complete_trade(client, app, accepted_trade):
    response = client.post(f'/trade/{accepted_trade}/complete')

    assert response.status_code == 302
    assert '/login' in response.headers['Location']
    with app.app_context():
        assert db.session.get(Trade, accepted_trade).status == Trade.STATUS_ACCEPTED


def test_unrelated_user_cannot_complete_trade(client, app, auth_helper, admin_user, accepted_trade):
    auth_helper.login('adminuser')

    response = client.post(f'/trade/{accepted_trade}/complete')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Trade, accepted_trade).status == Trade.STATUS_ACCEPTED


def test_seller_cannot_complete_when_buyer_confirmation_is_required(client, app, auth_helper, accepted_trade):
    auth_helper.login('testuser')

    response = client.post(f'/trade/{accepted_trade}/complete')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Trade, accepted_trade).status == Trade.STATUS_ACCEPTED


def test_buyer_can_complete_accepted_trade(client, app, auth_helper, normal_user, second_user, product, accepted_trade):
    with app.app_context():
        before_trade = db.session.get(Trade, accepted_trade)
        original = (before_trade.buyer_id, before_trade.seller_id, before_trade.product_id, before_trade.product.price)

    auth_helper.login('seconduser')
    response = client.post(f'/trade/{accepted_trade}/complete')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/trades')
    with app.app_context():
        after_trade = db.session.get(Trade, accepted_trade)
        assert after_trade.status == Trade.STATUS_COMPLETED
        assert (after_trade.buyer_id, after_trade.seller_id, after_trade.product_id, after_trade.product.price) == original


@pytest.mark.parametrize('status', [Trade.STATUS_PENDING, Trade.STATUS_REJECTED, Trade.STATUS_CANCELLED])
def test_non_accepted_trade_cannot_be_completed(client, app, auth_helper, normal_user, second_user, product, status):
    trade_id = _create_trade(app, buyer_id=second_user, seller_id=normal_user, product_id=product, status=status)
    auth_helper.login('seconduser')

    response = client.post(f'/trade/{trade_id}/complete')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Trade, trade_id).status == status


def test_completed_trade_repeated_action_is_idempotent_for_buyer(client, app, auth_helper, completed_trade):
    auth_helper.login('seconduser')

    response = client.post(f'/trade/{completed_trade}/complete')

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Trade, completed_trade).status == Trade.STATUS_COMPLETED


def test_get_completion_returns_405(client, accepted_trade):
    response = client.get(f'/trade/{accepted_trade}/complete')

    assert response.status_code == 405


def test_unauthenticated_user_cannot_access_review_creation(client, completed_trade):
    response = client.get(f'/trade/{completed_trade}/review')

    assert response.status_code == 302
    assert '/login' in response.headers['Location']


def test_unrelated_user_cannot_review(client, app, auth_helper, admin_user, completed_trade):
    auth_helper.login('adminuser')

    response = _post_review(client, completed_trade)

    assert response.status_code == 302
    assert _review_count(app) == 0


def test_participant_cannot_review_before_completion(client, app, auth_helper, accepted_trade):
    auth_helper.login('seconduser')

    response = _post_review(client, accepted_trade)

    assert response.status_code == 302
    assert _review_count(app) == 0


def test_buyer_can_review_seller_after_completion(client, app, auth_helper, normal_user, second_user, product, completed_trade):
    auth_helper.login('seconduser')

    response = _post_review(client, completed_trade, rating='5', content='친절한 판매자')

    assert response.status_code == 302
    with app.app_context():
        review = Review.query.one()
        assert review.reviewer_id == second_user
        assert review.reviewee_id == normal_user
        assert review.product_id == product
        assert review.trade_id == completed_trade
        assert review.rating == 5
        assert review.content == '친절한 판매자'


def test_seller_can_review_buyer_after_completion(client, app, auth_helper, normal_user, second_user, completed_trade):
    auth_helper.login('testuser')

    response = _post_review(client, completed_trade, rating='4', content='약속을 잘 지켰습니다.')

    assert response.status_code == 302
    with app.app_context():
        review = Review.query.one()
        assert review.reviewer_id == normal_user
        assert review.reviewee_id == second_user


def test_review_identity_fields_are_derived_from_trade_not_form(client, app, auth_helper, normal_user, second_user, product, completed_trade):
    forged_product_id = _create_trade(app, buyer_id=second_user, seller_id=normal_user, product_id=product, status=Trade.STATUS_PENDING)
    auth_helper.login('seconduser')

    response = _post_review(
        client,
        completed_trade,
        extra={
            'reviewer_id': normal_user,
            'reviewee_id': second_user,
            'product_id': forged_product_id,
            'trade_id': forged_product_id,
            'created_at': '2000-01-01',
        },
    )

    assert response.status_code == 302
    with app.app_context():
        review = Review.query.one()
        assert review.reviewer_id == second_user
        assert review.reviewee_id == normal_user
        assert review.trade_id == completed_trade
        assert review.product_id == product


@pytest.mark.parametrize('rating', ['0', '6', 'abc'])
def test_invalid_rating_is_rejected(client, app, auth_helper, completed_trade, rating):
    auth_helper.login('seconduser')

    response = _post_review(client, completed_trade, rating=rating)

    assert response.status_code == 302
    assert _review_count(app) == 0


@pytest.mark.parametrize('content', ['가' * 501, '좋은\n거래', '나쁜\x00값'])
def test_invalid_content_is_rejected(client, app, auth_helper, completed_trade, content):
    auth_helper.login('seconduser')

    response = _post_review(client, completed_trade, content=content)

    assert response.status_code == 302
    assert _review_count(app) == 0


def test_duplicate_review_by_same_reviewer_is_rejected(client, app, auth_helper, completed_trade):
    auth_helper.login('seconduser')
    first = _post_review(client, completed_trade, rating='5')
    second = _post_review(client, completed_trade, rating='4')

    assert first.status_code == 302
    assert second.status_code == 302
    assert _review_count(app) == 1


def test_other_participant_may_still_create_one_review(client, app, auth_helper, completed_trade):
    auth_helper.login('seconduser')
    _post_review(client, completed_trade, rating='5')
    auth_helper.login('testuser')

    response = _post_review(client, completed_trade, rating='4')

    assert response.status_code == 302
    assert _review_count(app) == 2


def test_get_review_form_works_only_for_eligible_participant(
    client,
    app,
    auth_helper,
    normal_user,
    second_user,
    completed_trade,
):
    accepted_product_id = _create_other_product(app, normal_user, 'Accepted Only Product')
    accepted_only_trade = _create_trade(
        app,
        buyer_id=second_user,
        seller_id=normal_user,
        product_id=accepted_product_id,
        status=Trade.STATUS_ACCEPTED,
    )
    auth_helper.login('seconduser')
    eligible_response = client.get(f'/trade/{completed_trade}/review')
    ineligible_response = client.get(f'/trade/{accepted_only_trade}/review')

    assert eligible_response.status_code == 200
    assert '거래 리뷰 작성' in eligible_response.get_data(as_text=True)
    assert ineligible_response.status_code == 302


def test_post_without_csrf_is_rejected_in_csrf_enabled_app(csrf_app, csrf_client):
    with csrf_app.app_context():
        seller_id = _create_user('seller')
        buyer_id = _create_user('buyer')
        product = Product(seller_id=seller_id, title='CSRF Product', description='Desc', price=1000, status=Product.STATUS_SOLD)
        db.session.add(product)
        db.session.commit()
        trade = Trade(product_id=product.id, buyer_id=buyer_id, seller_id=seller_id, status=Trade.STATUS_COMPLETED)
        db.session.add(trade)
        db.session.commit()
        trade_id = trade.id

    with csrf_client.session_transaction() as sess:
        sess['user_id'] = buyer_id

    response = csrf_client.post(f'/trade/{trade_id}/review', data={'rating': '5', 'content': 'csrf'})

    assert response.status_code == 400


def test_review_creation_uses_post_only_for_mutation(client, app, auth_helper, completed_trade):
    auth_helper.login('seconduser')

    response = client.get(f'/trade/{completed_trade}/review')

    assert response.status_code == 200
    assert _review_count(app) == 0


def test_duplicate_review_database_constraint_exists(app, normal_user, second_user, product, completed_trade):
    with app.app_context():
        review1 = Review(
            trade_id=completed_trade,
            product_id=product,
            reviewer_id=second_user,
            reviewee_id=normal_user,
            rating=5,
        )
        review2 = Review(
            trade_id=completed_trade,
            product_id=product,
            reviewer_id=second_user,
            reviewee_id=normal_user,
            rating=4,
        )
        db.session.add(review1)
        db.session.commit()
        db.session.add(review2)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        assert Review.query.count() == 1


def test_self_review_is_prevented_at_database_level(app, normal_user, product, completed_trade):
    with app.app_context():
        review = Review(
            trade_id=completed_trade,
            product_id=product,
            reviewer_id=normal_user,
            reviewee_id=normal_user,
            rating=5,
        )
        db.session.add(review)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        assert Review.query.count() == 0


def test_failed_review_creation_rolls_back_safely(client, app, auth_helper, completed_trade):
    auth_helper.login('seconduser')

    response = _post_review(client, completed_trade, rating='5', content='bad\ncontent')

    assert response.status_code == 302
    assert _review_count(app) == 0


def test_product_page_displays_only_associated_product_reviews(client, app, normal_user, second_user, product, completed_trade):
    other_product_id = _create_other_product(app, normal_user, 'Other Reviewed Product')
    with app.app_context():
        other_trade = Trade(product_id=other_product_id, buyer_id=second_user, seller_id=normal_user, status=Trade.STATUS_COMPLETED)
        db.session.add(other_trade)
        db.session.commit()
        db.session.add(Review(trade_id=completed_trade, product_id=product, reviewer_id=second_user, reviewee_id=normal_user, rating=5, content='<b>safe</b>'))
        db.session.add(Review(trade_id=other_trade.id, product_id=other_product_id, reviewer_id=second_user, reviewee_id=normal_user, rating=1, content='Other product review'))
        db.session.commit()

    response = client.get(f'/product/{product}')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert escape('<b>safe</b>') in body
    assert 'Other product review' not in body
    assert '평균 평점: 5.0 / 5' in body


def _create_other_product(app, seller_id, title):
    with app.app_context():
        product = Product(seller_id=seller_id, title=title, description='Desc', price=2000, status=Product.STATUS_SOLD)
        db.session.add(product)
        db.session.commit()
        return product.id


def test_user_review_page_displays_received_reviews_only_and_average(client, app, normal_user, second_user, product, completed_trade):
    other_product_id = _create_other_product(app, normal_user, 'Second Reviewed Product')
    with app.app_context():
        other_trade = Trade(product_id=other_product_id, buyer_id=second_user, seller_id=normal_user, status=Trade.STATUS_COMPLETED)
        db.session.add(other_trade)
        db.session.commit()
        db.session.add(Review(trade_id=completed_trade, product_id=product, reviewer_id=second_user, reviewee_id=normal_user, rating=5, content='Great seller'))
        db.session.add(Review(trade_id=other_trade.id, product_id=other_product_id, reviewer_id=second_user, reviewee_id=normal_user, rating=3, content='Okay seller'))
        db.session.add(Review(trade_id=completed_trade, product_id=product, reviewer_id=normal_user, reviewee_id=second_user, rating=1, content='Buyer review'))
        db.session.commit()

    seller_response = client.get(f'/user/{normal_user}/reviews')
    buyer_response = client.get(f'/user/{second_user}/reviews')

    assert seller_response.status_code == 200
    seller_body = seller_response.get_data(as_text=True)
    assert 'Great seller' in seller_body
    assert 'Okay seller' in seller_body
    assert 'Buyer review' not in seller_body
    assert '평균 평점: 4.0 / 5' in seller_body
    assert buyer_response.status_code == 200
    assert 'Buyer review' in buyer_response.get_data(as_text=True)


def test_review_empty_state_and_invalid_user_id_are_safe(client, normal_user):
    empty_response = client.get(f'/user/{normal_user}/reviews')
    invalid_response = client.get('/user/not-a-uuid/reviews')

    assert empty_response.status_code == 200
    assert '아직 받은 리뷰가 없습니다.' in empty_response.get_data(as_text=True)
    assert invalid_response.status_code == 404


def test_review_pages_do_not_display_sensitive_fields(client, app, normal_user, second_user, product, completed_trade):
    with app.app_context():
        db.session.add(Review(trade_id=completed_trade, product_id=product, reviewer_id=second_user, reviewee_id=normal_user, rating=5, content='Safe'))
        db.session.commit()

    product_body = client.get(f'/product/{product}').get_data(as_text=True)
    user_body = client.get(f'/user/{normal_user}/reviews').get_data(as_text=True)

    for body in (product_body, user_body):
        assert 'password_hash' not in body
        assert 'TestPassword123!' not in body
        assert 'csrf-review-secret' not in body
        assert 'BAN' not in body
        assert 'session' not in body


def _create_base_tables_for_migration(engine):
    User.__table__.create(bind=engine)
    Product.__table__.create(bind=engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO user (id, username, password_hash, bio, role, is_active, created_at) "
            "VALUES "
            "('seller', 'seller', 'hash', NULL, 'USER', 1, '2026-01-01 00:00:00'), "
            "('buyer', 'buyer', 'hash', NULL, 'USER', 1, '2026-01-01 00:00:00')"
        ))
        connection.execute(text(
            "INSERT INTO product "
            "(id, seller_id, title, description, price, trade_location, status, created_at, updated_at) "
            "VALUES "
            "('product', 'seller', 'Product', 'Desc', 1000, NULL, 'SOLD', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        ))
        connection.execute(text("CREATE TABLE unrelated_table (id INTEGER PRIMARY KEY, note TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO unrelated_table (id, note) VALUES (1, 'keep me')"))


def _create_legacy_trade_table(engine):
    with engine.begin() as connection:
        connection.execute(text(
            'CREATE TABLE trade ('
            'id VARCHAR(36) NOT NULL, '
            'product_id VARCHAR(36) NOT NULL, '
            'buyer_id VARCHAR(36) NOT NULL, '
            'seller_id VARCHAR(36) NOT NULL, '
            'status VARCHAR(20) NOT NULL, '
            'created_at DATETIME NOT NULL, '
            'updated_at DATETIME NOT NULL, '
            'PRIMARY KEY (id), '
            'CONSTRAINT ck_trade_buyer_ne_seller CHECK (buyer_id != seller_id), '
            "CONSTRAINT ck_trade_status CHECK (status IN ('PENDING', 'ACCEPTED', 'REJECTED', 'CANCELLED')), "
            'FOREIGN KEY(product_id) REFERENCES product (id), '
            'FOREIGN KEY(buyer_id) REFERENCES "user" (id), '
            'FOREIGN KEY(seller_id) REFERENCES "user" (id)'
            ')'
        ))
        connection.execute(text('CREATE INDEX ix_trade_buyer_id ON trade (buyer_id)'))
        connection.execute(text('CREATE INDEX ix_trade_product_id ON trade (product_id)'))
        connection.execute(text('CREATE INDEX ix_trade_seller_id ON trade (seller_id)'))


def _insert_legacy_trade_row(engine, trade_id='trade', status=Trade.STATUS_ACCEPTED):
    with engine.begin() as connection:
        connection.execute(text(
            'INSERT INTO trade (id, product_id, buyer_id, seller_id, status, created_at, updated_at) '
            'VALUES (:id, :product_id, :buyer_id, :seller_id, :status, :created_at, :updated_at)'
        ), {
            'id': trade_id,
            'product_id': 'product',
            'buyer_id': 'buyer',
            'seller_id': 'seller',
            'status': status,
            'created_at': '2026-01-02 03:04:05',
            'updated_at': '2026-01-03 04:05:06',
        })


def _trade_rows(engine):
    with engine.connect() as connection:
        rows = connection.execute(text(
            'SELECT id, product_id, buyer_id, seller_id, status, created_at, updated_at '
            'FROM trade ORDER BY id'
        )).all()
    return [tuple(row) for row in rows]


def _unrelated_rows(engine):
    with engine.connect() as connection:
        rows = connection.execute(text('SELECT id, note FROM unrelated_table ORDER BY id')).all()
    return [tuple(row) for row in rows]


def _review_table_sql(engine):
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'review'")
        ).scalar()


def _trade_index_names(engine):
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'trade' AND sql IS NOT NULL "
            "ORDER BY name"
        )).all()
    return [row[0] for row in rows]


def _set_sqlite_foreign_keys(engine, enabled):
    with engine.connect() as connection:
        connection.execute(text(f'PRAGMA foreign_keys={"ON" if enabled else "OFF"}'))
        connection.commit()
        return int(connection.execute(text('PRAGMA foreign_keys')).scalar())


def _sqlite_foreign_keys(engine):
    with engine.connect() as connection:
        return int(connection.execute(text('PRAGMA foreign_keys')).scalar())


def test_full_migration_updates_legacy_trade_constraint_creates_review_table_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy_reviews.sqlite'}")
    _create_base_tables_for_migration(engine)
    _create_legacy_trade_table(engine)
    _insert_legacy_trade_row(engine)
    before_rows = _trade_rows(engine)
    original_foreign_keys = _sqlite_foreign_keys(engine)

    first = migrate_reviews_schema(engine)
    second = migrate_reviews_schema(engine)

    assert first == 0
    assert second == 0
    assert _trade_rows(engine) == before_rows
    assert _unrelated_rows(engine) == [(1, 'keep me')]
    assert trade_status_supports_completed(engine) is True
    assert _sqlite_foreign_keys(engine) == original_foreign_keys
    assert 'review' in inspect(engine).get_table_names()
    review_sql = _review_table_sql(engine)
    assert 'UNIQUE (trade_id, reviewer_id)' in review_sql
    assert 'rating >= 1 AND rating <= 5' in review_sql
    assert 'reviewer_id != reviewee_id' in review_sql

    with engine.connect() as connection:
        connection.execute(text(
            "INSERT INTO trade (id, product_id, buyer_id, seller_id, status, created_at, updated_at) "
            "VALUES ('completed_trade', 'product', 'buyer', 'seller', 'COMPLETED', "
            "'2026-01-04 00:00:00', '2026-01-04 00:00:00')"
        ))
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO trade (id, product_id, buyer_id, seller_id, status, created_at, updated_at) "
                "VALUES ('bad_trade', 'product', 'buyer', 'seller', 'INVALID', "
                "'2026-01-04 00:00:00', '2026-01-04 00:00:00')"
            ))


def test_trade_status_migration_rolls_back_when_foreign_key_check_fails(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'broken_fk.sqlite'}")
    _create_base_tables_for_migration(engine)
    _create_legacy_trade_table(engine)
    _insert_legacy_trade_row(engine, trade_id='valid_trade')
    _insert_legacy_trade_row(engine, trade_id='broken_trade')
    with engine.connect() as connection:
        connection.commit()
        connection.execute(text('PRAGMA foreign_keys=OFF'))
        connection.commit()
        connection.execute(text("UPDATE trade SET product_id = 'missing_product' WHERE id = 'broken_trade'"))
        connection.commit()
    before_rows = _trade_rows(engine)
    before_indexes = _trade_index_names(engine)
    assert _set_sqlite_foreign_keys(engine, True) == 1

    result = migrate_reviews_schema(engine)

    assert result == 1
    assert _trade_rows(engine) == before_rows
    assert _trade_index_names(engine) == before_indexes
    assert trade_status_supports_completed(engine) is False
    assert _sqlite_foreign_keys(engine) == 1
    table_names = inspect(engine).get_table_names()
    assert 'trade' in table_names
    assert 'trade_new' not in table_names
    assert 'review' not in table_names
    with engine.connect() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO trade (id, product_id, buyer_id, seller_id, status, created_at, updated_at) "
                "VALUES ('completed_rejected', 'product', 'buyer', 'seller', 'COMPLETED', "
                "'2026-01-04 00:00:00', '2026-01-04 00:00:00')"
            ))


def test_trade_status_migration_stops_when_trade_new_already_exists(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'preexisting_trade_new.sqlite'}")
    _create_base_tables_for_migration(engine)
    _create_legacy_trade_table(engine)
    _insert_legacy_trade_row(engine)
    before_rows = _trade_rows(engine)
    before_indexes = _trade_index_names(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE trade_new (id VARCHAR(36) PRIMARY KEY, note TEXT)"))
        connection.execute(text("INSERT INTO trade_new (id, note) VALUES ('recovery', 'preserve')"))

    result = migrate_reviews_schema(engine)

    assert result == 2
    assert _trade_rows(engine) == before_rows
    assert _trade_index_names(engine) == before_indexes
    assert trade_status_supports_completed(engine) is False
    table_names = inspect(engine).get_table_names()
    assert 'trade_new' in table_names
    assert 'review' not in table_names
    with engine.connect() as connection:
        trade_new_rows = connection.execute(text('SELECT id, note FROM trade_new')).all()
    assert [tuple(row) for row in trade_new_rows] == [('recovery', 'preserve')]


def test_full_migration_handles_legacy_trade_with_existing_review_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy_existing_review.sqlite'}")
    _create_base_tables_for_migration(engine)
    _create_legacy_trade_table(engine)
    _insert_legacy_trade_row(engine)
    Review.__table__.create(bind=engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO review (id, trade_id, product_id, reviewer_id, reviewee_id, rating, content, created_at) "
            "VALUES ('review1', 'trade', 'product', 'buyer', 'seller', 5, 'existing', '2026-01-05 00:00:00')"
        ))

    result = migrate_reviews_schema(engine)

    assert result == 0
    assert trade_status_supports_completed(engine) is True
    with engine.connect() as connection:
        review_rows = connection.execute(text('SELECT id, content FROM review')).all()
    assert [tuple(row) for row in review_rows] == [('review1', 'existing')]


def test_full_migration_handles_updated_trade_without_review_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'updated_no_review.sqlite'}")
    _create_base_tables_for_migration(engine)
    Trade.__table__.create(bind=engine)
    _insert_legacy_trade_row(engine)

    result = migrate_reviews_schema(engine)

    assert result == 0
    assert trade_status_supports_completed(engine) is True
    assert 'review' in inspect(engine).get_table_names()


def test_full_migration_handles_updated_trade_with_existing_review_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'updated_existing_review.sqlite'}")
    _create_base_tables_for_migration(engine)
    Trade.__table__.create(bind=engine)
    _insert_legacy_trade_row(engine)
    Review.__table__.create(bind=engine)

    result = migrate_reviews_schema(engine)

    assert result == 0
    assert trade_status_supports_completed(engine) is True
    assert 'review' in inspect(engine).get_table_names()


def test_full_migration_stops_safely_for_incompatible_existing_review_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_review.sqlite'}")
    _create_base_tables_for_migration(engine)
    _create_legacy_trade_table(engine)
    _insert_legacy_trade_row(engine)
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE review (id VARCHAR(36) PRIMARY KEY)'))

    result = migrate_reviews_schema(engine)

    assert result == 2
    assert trade_status_supports_completed(engine) is False


def test_full_migration_stops_safely_for_non_sqlite_database():
    class FakeDialect:
        name = 'postgresql'

    class FakeEngine:
        dialect = FakeDialect()

    assert migrate_reviews_schema(FakeEngine()) == 2
    assert create_reviews_table(FakeEngine()) == 2
