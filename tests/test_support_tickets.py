import re

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from market import create_app
from market.extensions import db
from market.models import Notification, Product, Report, SupportTicket, User
from scripts.add_support_tickets import (
    _constraint_values_for_column,
    migrate_support_tickets_schema,
    support_ticket_table_is_compatible,
)


TEST_PASSWORD = 'TestPassword123!'


@pytest.fixture
def csrf_app():
    app = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': True,
        'RATELIMIT_ENABLED': False,
        'SECRET_KEY': 'csrf-support-secret',
    })
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def csrf_client(csrf_app):
    return csrf_app.test_client()


@pytest.fixture
def rate_app():
    app = create_app(test_config={
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': False,
        'RATELIMIT_ENABLED': True,
        'RATELIMIT_STORAGE_URI': 'memory://',
        'SECRET_KEY': 'rate-support-secret',
    })
    with app.app_context():
        db.create_all()
        user = User(username='rateuser', role=User.ROLE_USER, is_active=True)
        user.set_password(TEST_PASSWORD)
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        yield app, user_id
        db.session.remove()
        db.drop_all()


@pytest.fixture
def rate_client(rate_app):
    app, _user_id = rate_app
    return app.test_client()


def _csrf_token(response):
    match = re.search(
        r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']',
        response.get_data(as_text=True),
    )
    assert match is not None
    return match.group(1)


def _login_session(client, user_id):
    with client.session_transaction() as sess:
        sess['user_id'] = user_id


def _create_user(username, role=User.ROLE_USER, is_active=True):
    user = User(username=username, role=role, is_active=is_active)
    user.set_password(TEST_PASSWORD)
    db.session.add(user)
    db.session.commit()
    return user.id


def _support_data(category=SupportTicket.CATEGORY_BUG, title='문의 제목', content='문의 내용', extra=None):
    data = {
        'category': category,
        'title': title,
        'content': content,
    }
    if extra:
        data.update(extra)
    return data


def _admin_update_data(status=SupportTicket.STATUS_IN_PROGRESS, admin_response='확인 중입니다.', extra=None):
    data = {
        'status': status,
        'admin_response': admin_response,
    }
    if extra:
        data.update(extra)
    return data


def _create_ticket(user_id, category=SupportTicket.CATEGORY_BUG, title='문의 제목', content='문의 내용', status=SupportTicket.STATUS_OPEN, admin_response=None):
    ticket = SupportTicket(
        user_id=user_id,
        category=category,
        title=title,
        content=content,
        status=status,
        admin_response=admin_response,
    )
    db.session.add(ticket)
    db.session.commit()
    return ticket.id


def _ticket_count():
    return SupportTicket.query.count()


def test_unauthenticated_user_cannot_access_support(client):
    response = client.get('/support')

    assert response.status_code == 302
    assert '/login' in response.headers['Location']


def test_active_authenticated_user_can_access_support(client, auth_helper, normal_user):
    auth_helper.login('testuser')

    response = client.get('/support')

    assert response.status_code == 200
    assert '고객지원' in response.get_data(as_text=True)


def test_inactive_user_cannot_access_support_routes(client, inactive_user):
    _login_session(client, inactive_user)

    list_response = client.get('/support')
    new_response = client.get('/support/new')

    assert list_response.status_code == 302
    assert new_response.status_code == 302
    assert '/login' in list_response.headers['Location']


def test_valid_korean_support_ticket_can_be_created_and_owner_is_session_user(client, app, auth_helper, normal_user, second_user):
    auth_helper.login('testuser')

    response = client.post('/support/new', data=_support_data(
        category=SupportTicket.CATEGORY_TRADE,
        title='거래 문의',
        content='거래 중 문제가 있습니다.',
        extra={'user_id': second_user},
    ))

    assert response.status_code == 302
    with app.app_context():
        ticket = SupportTicket.query.one()
        assert ticket.user_id == normal_user
        assert ticket.user_id != second_user
        assert ticket.category == SupportTicket.CATEGORY_TRADE
        assert ticket.title == '거래 문의'
        assert ticket.content == '거래 중 문제가 있습니다.'
        assert ticket.status == SupportTicket.STATUS_OPEN


@pytest.mark.parametrize('category', [
    SupportTicket.CATEGORY_BUG,
    SupportTicket.CATEGORY_TRADE,
    SupportTicket.CATEGORY_ACCOUNT,
    SupportTicket.CATEGORY_OTHER,
])
def test_valid_category_values_are_accepted(client, app, auth_helper, normal_user, category):
    auth_helper.login('testuser')

    response = client.post('/support/new', data=_support_data(category=category, title=f'{category} 문의'))

    assert response.status_code == 302
    with app.app_context():
        assert SupportTicket.query.filter_by(category=category).count() == 1


@pytest.mark.parametrize('category', ['', 'UNKNOWN', 'other'])
def test_invalid_category_is_rejected(client, app, auth_helper, normal_user, category):
    auth_helper.login('testuser')

    response = client.post('/support/new', data=_support_data(category=category))

    assert response.status_code == 302
    with app.app_context():
        assert _ticket_count() == 0


@pytest.mark.parametrize('title', ['', '   ', '가' * 121, 'bad\nvalue', 'bad\tvalue', 'bad\x00value', 'bad\u2028value', 'bad\u2029value', 'bad\uE000value'])
def test_invalid_titles_are_rejected(client, app, auth_helper, normal_user, title):
    auth_helper.login('testuser')

    response = client.post('/support/new', data=_support_data(title=title))

    assert response.status_code == 302
    with app.app_context():
        assert _ticket_count() == 0


@pytest.mark.parametrize('content', ['', '   ', '가' * 2001, 'bad\x00value', 'bad\tvalue', 'bad\u2028value', 'bad\u2029value', 'bad\uE000value'])
def test_invalid_content_is_rejected(client, app, auth_helper, normal_user, content):
    auth_helper.login('testuser')

    response = client.post('/support/new', data=_support_data(content=content))

    assert response.status_code == 302
    with app.app_context():
        assert _ticket_count() == 0


def test_ordinary_korean_content_with_lf_line_breaks_is_accepted(client, app, auth_helper, normal_user):
    auth_helper.login('testuser')

    response = client.post('/support/new', data=_support_data(
        title='줄바꿈 문의',
        content='첫 줄입니다.\r\n두 번째 줄입니다.\r세 번째 줄입니다.',
    ))

    assert response.status_code == 302
    with app.app_context():
        ticket = SupportTicket.query.one()
        assert ticket.content == '첫 줄입니다.\n두 번째 줄입니다.\n세 번째 줄입니다.'


def test_failed_creation_creates_no_row(client, app, auth_helper, normal_user):
    auth_helper.login('testuser')

    response = client.post('/support/new', data=_support_data(title=''))

    assert response.status_code == 302
    with app.app_context():
        assert _ticket_count() == 0


def test_ticket_creation_uses_post(client):
    response = client.get('/support/new')

    assert response.status_code == 302


def test_csrf_protects_support_creation(csrf_app, csrf_client):
    with csrf_app.app_context():
        user_id = _create_user('csrfuser')
    _login_session(csrf_client, user_id)

    without_token = csrf_client.post('/support/new', data=_support_data())
    token = _csrf_token(csrf_client.get('/support/new'))
    with_token = csrf_client.post('/support/new', data={**_support_data(title='CSRF 문의'), 'csrf_token': token})

    assert without_token.status_code == 400
    assert with_token.status_code == 302


def test_creation_rate_limit_is_applied(rate_app, rate_client):
    _app, user_id = rate_app
    _login_session(rate_client, user_id)

    responses = [
        rate_client.post('/support/new', data=_support_data(title=f'문의 {index}'))
        for index in range(6)
    ]

    assert [response.status_code for response in responses[:5]] == [302, 302, 302, 302, 302]
    assert responses[5].status_code == 429


def test_user_sees_only_their_own_ticket_in_list(client, app, auth_helper, normal_user, second_user):
    with app.app_context():
        _create_ticket(normal_user, title='내 문의')
        _create_ticket(second_user, title='다른 사람 문의')
    auth_helper.login('testuser')

    response = client.get('/support')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '내 문의' in body
    assert '다른 사람 문의' not in body


def test_user_can_view_own_ticket_but_not_another_users_ticket(client, app, auth_helper, normal_user, second_user):
    with app.app_context():
        own_ticket = _create_ticket(normal_user, title='내 상세 문의')
        other_ticket = _create_ticket(second_user, title='타인 상세 문의')
    auth_helper.login('testuser')

    own_response = client.get(f'/support/{own_ticket}')
    other_response = client.get(f'/support/{other_ticket}')

    assert own_response.status_code == 200
    assert '내 상세 문의' in own_response.get_data(as_text=True)
    assert other_response.status_code == 302
    assert other_response.headers['Location'].endswith('/support')
    assert '타인 상세 문의' not in other_response.get_data(as_text=True)


def test_forged_url_ticket_uuid_does_not_bypass_ownership(client, app, auth_helper, normal_user, second_user):
    with app.app_context():
        other_ticket = _create_ticket(second_user, title='비공개 문의')
    auth_helper.login('testuser')

    response = client.get(f'/support/{other_ticket}')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/support')


def test_administrator_can_view_ticket_through_admin_authorization(client, app, auth_helper, normal_user, admin_user):
    with app.app_context():
        ticket_id = _create_ticket(normal_user, title='관리자 조회 문의')
    auth_helper.login('adminuser')

    response = client.get(f'/admin/support/{ticket_id}')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '관리자 조회 문의' in body
    assert 'testuser' in body


def test_normal_user_cannot_access_admin_support_routes(client, app, auth_helper, normal_user):
    with app.app_context():
        ticket_id = _create_ticket(normal_user, title='관리자 접근 금지')
    auth_helper.login('testuser')

    list_response = client.get('/admin/support')
    detail_response = client.get(f'/admin/support/{ticket_id}')
    update_response = client.post(f'/admin/support/{ticket_id}/update', data=_admin_update_data())

    assert list_response.status_code == 302
    assert detail_response.status_code == 302
    assert update_response.status_code == 302
    with app.app_context():
        assert db.session.get(SupportTicket, ticket_id).status == SupportTicket.STATUS_OPEN


def test_rendered_support_content_is_escaped_and_line_breaks_are_preserved(client, app, auth_helper, normal_user):
    with app.app_context():
        ticket_id = _create_ticket(
            normal_user,
            title='<script>제목</script>',
            content='<b>첫 줄</b>\n두 번째 줄',
            admin_response='<i>답변</i>\n다음 줄',
        )
    auth_helper.login('testuser')

    body = client.get(f'/support/{ticket_id}').get_data(as_text=True)

    assert '&lt;script&gt;제목&lt;/script&gt;' in body
    assert '&lt;b&gt;첫 줄&lt;/b&gt;' in body
    assert '&lt;i&gt;답변&lt;/i&gt;' in body
    assert '<script>제목</script>' not in body
    assert '<b>첫 줄</b>' not in body
    assert 'class="prewrap"' in body
    assert '두 번째 줄' in body


def test_support_pages_do_not_display_sensitive_fields(client, app, auth_helper, normal_user):
    with app.app_context():
        ticket_id = _create_ticket(normal_user, title='민감정보 점검', content='민감하지 않은 문의')
    auth_helper.login('testuser')

    body = client.get(f'/support/{ticket_id}').get_data(as_text=True)

    for sensitive in ('password_hash', TEST_PASSWORD, 'csrf-support-secret', 'role', 'BAN', 'session', '신고 사유', '민감정보 점검 ID'):
        assert sensitive not in body


def test_ticket_content_does_not_appear_in_unrelated_product_or_notification_pages(
    client,
    app,
    auth_helper,
    normal_user,
):
    with app.app_context():
        _create_ticket(normal_user, title='비공개지원제목', content='비공개지원내용')
    auth_helper.login('testuser')

    dashboard_body = client.get('/dashboard').get_data(as_text=True)
    notifications_body = client.get('/notifications').get_data(as_text=True)

    assert '비공개지원제목' not in dashboard_body
    assert '비공개지원내용' not in dashboard_body
    assert '비공개지원제목' not in notifications_body
    assert '비공개지원내용' not in notifications_body


def test_admin_can_list_and_filter_support_tickets(client, app, auth_helper, normal_user, second_user, admin_user):
    with app.app_context():
        _create_ticket(normal_user, title='열린 문의', status=SupportTicket.STATUS_OPEN)
        _create_ticket(second_user, title='해결 문의', status=SupportTicket.STATUS_RESOLVED)
    auth_helper.login('adminuser')

    all_response = client.get('/admin/support')
    filtered_response = client.get(f'/admin/support?status={SupportTicket.STATUS_RESOLVED}')
    invalid_filter_response = client.get("/admin/support?status=RESOLVED' OR '1'='1")

    assert all_response.status_code == 200
    assert '열린 문의' in all_response.get_data(as_text=True)
    assert '해결 문의' in all_response.get_data(as_text=True)
    filtered_body = filtered_response.get_data(as_text=True)
    assert '해결 문의' in filtered_body
    assert '열린 문의' not in filtered_body
    invalid_body = invalid_filter_response.get_data(as_text=True)
    assert '열린 문의' in invalid_body
    assert '해결 문의' in invalid_body


@pytest.mark.parametrize('status', [
    SupportTicket.STATUS_IN_PROGRESS,
    SupportTicket.STATUS_RESOLVED,
    SupportTicket.STATUS_OPEN,
])
def test_admin_can_update_statuses_and_response(client, app, auth_helper, normal_user, admin_user, status):
    with app.app_context():
        ticket_id = _create_ticket(normal_user, status=SupportTicket.STATUS_RESOLVED)
    auth_helper.login('adminuser')

    response = client.post(f'/admin/support/{ticket_id}/update', data=_admin_update_data(
        status=status,
        admin_response='답변입니다.\r\n다음 줄',
    ))

    assert response.status_code == 302
    with app.app_context():
        ticket = db.session.get(SupportTicket, ticket_id)
        assert ticket.status == status
        assert ticket.admin_response == '답변입니다.\n다음 줄'


def test_normal_user_cannot_set_status_or_response(client, app, auth_helper, normal_user):
    with app.app_context():
        ticket_id = _create_ticket(normal_user)
    auth_helper.login('testuser')

    response = client.post(f'/admin/support/{ticket_id}/update', data=_admin_update_data(
        status=SupportTicket.STATUS_RESOLVED,
        admin_response='사용자 위조 답변',
        extra={'role': User.ROLE_ADMIN, 'user_id': normal_user},
    ))

    assert response.status_code == 302
    with app.app_context():
        ticket = db.session.get(SupportTicket, ticket_id)
        assert ticket.status == SupportTicket.STATUS_OPEN
        assert ticket.admin_response is None


@pytest.mark.parametrize('status', ['INVALID', 'resolved'])
def test_unknown_status_is_rejected(client, app, auth_helper, normal_user, admin_user, status):
    with app.app_context():
        ticket_id = _create_ticket(normal_user)
    auth_helper.login('adminuser')

    response = client.post(f'/admin/support/{ticket_id}/update', data=_admin_update_data(status=status))

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(SupportTicket, ticket_id).status == SupportTicket.STATUS_OPEN


@pytest.mark.parametrize('response_text', ['가' * 2001, 'bad\x00value', 'bad\tvalue', 'bad\u2028value', 'bad\u2029value', 'bad\uE000value'])
def test_invalid_admin_response_is_rejected(client, app, auth_helper, normal_user, admin_user, response_text):
    with app.app_context():
        ticket_id = _create_ticket(normal_user)
    auth_helper.login('adminuser')

    response = client.post(f'/admin/support/{ticket_id}/update', data=_admin_update_data(
        status=SupportTicket.STATUS_IN_PROGRESS,
        admin_response=response_text,
    ))

    assert response.status_code == 302
    with app.app_context():
        ticket = db.session.get(SupportTicket, ticket_id)
        assert ticket.status == SupportTicket.STATUS_OPEN
        assert ticket.admin_response is None


def test_blank_admin_response_becomes_none_and_repeated_update_is_idempotent(client, app, auth_helper, normal_user, admin_user):
    with app.app_context():
        ticket_id = _create_ticket(normal_user)
    auth_helper.login('adminuser')

    first = client.post(f'/admin/support/{ticket_id}/update', data=_admin_update_data(
        status=SupportTicket.STATUS_IN_PROGRESS,
        admin_response='',
    ))
    second = client.post(f'/admin/support/{ticket_id}/update', data=_admin_update_data(
        status=SupportTicket.STATUS_IN_PROGRESS,
        admin_response='',
    ))

    assert first.status_code == 302
    assert second.status_code == 302
    with app.app_context():
        ticket = db.session.get(SupportTicket, ticket_id)
        assert ticket.status == SupportTicket.STATUS_IN_PROGRESS
        assert ticket.admin_response is None


def test_get_admin_update_route_returns_405(client, app, auth_helper, normal_user, admin_user):
    with app.app_context():
        ticket_id = _create_ticket(normal_user)
    auth_helper.login('adminuser')

    response = client.get(f'/admin/support/{ticket_id}/update')

    assert response.status_code == 405


def test_csrf_protects_admin_update(csrf_app, csrf_client):
    with csrf_app.app_context():
        user_id = _create_user('ticketuser')
        admin_id = _create_user('ticketadmin', role=User.ROLE_ADMIN)
        ticket_id = _create_ticket(user_id)
    _login_session(csrf_client, admin_id)

    without_token = csrf_client.post(f'/admin/support/{ticket_id}/update', data=_admin_update_data())
    token = _csrf_token(csrf_client.get(f'/admin/support/{ticket_id}'))
    with_token = csrf_client.post(f'/admin/support/{ticket_id}/update', data={
        **_admin_update_data(status=SupportTicket.STATUS_RESOLVED),
        'csrf_token': token,
    })

    assert without_token.status_code == 400
    assert with_token.status_code == 302


def test_database_failure_rolls_back_admin_update(client, app, auth_helper, normal_user, admin_user, monkeypatch):
    with app.app_context():
        ticket_id = _create_ticket(normal_user, status=SupportTicket.STATUS_OPEN, admin_response=None)
    auth_helper.login('adminuser')

    def fail_commit():
        raise SQLAlchemyError()

    with monkeypatch.context() as patch:
        patch.setattr(db.session, 'commit', fail_commit)
        response = client.post(f'/admin/support/{ticket_id}/update', data=_admin_update_data(
            status=SupportTicket.STATUS_RESOLVED,
            admin_response='실패해야 합니다.',
        ))

    assert response.status_code == 302
    body = response.get_data(as_text=True)
    assert 'sqlalchemy' not in body.lower()
    assert '/home/' not in body
    with app.app_context():
        ticket = db.session.get(SupportTicket, ticket_id)
        assert ticket.status == SupportTicket.STATUS_OPEN
        assert ticket.admin_response is None


def test_existing_abuse_report_model_and_routes_remain_separate(client, app, auth_helper, normal_user, second_user):
    auth_helper.login('testuser')

    support_response = client.post('/support/new', data=_support_data(title='지원 문의'))
    report_response = client.post('/report', data={
        'target_type': Report.TARGET_USER,
        'target_id': second_user,
        'reason': 'Spam',
    })

    assert support_response.status_code == 302
    assert report_response.status_code == 302
    with app.app_context():
        assert SupportTicket.query.count() == 1
        assert Report.query.count() == 1


def _migration_base_tables(engine):
    User.__table__.create(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE unrelated_table (id INTEGER PRIMARY KEY, note TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO unrelated_table (id, note) VALUES (1, 'preserve')"))
        connection.execute(text(
            "INSERT INTO user (id, username, password_hash, bio, role, is_active, created_at) "
            "VALUES ('user1', 'user1', 'hash', NULL, 'USER', 1, '2026-01-01 00:00:00')"
        ))


def _support_rows(engine):
    with engine.connect() as connection:
        rows = connection.execute(text(
            'SELECT id, user_id, category, title, content, status, admin_response, created_at, updated_at '
            'FROM support_ticket ORDER BY id'
        )).all()
    return [tuple(row) for row in rows]


def _unrelated_rows(engine):
    with engine.connect() as connection:
        rows = connection.execute(text('SELECT id, note FROM unrelated_table ORDER BY id')).all()
    return [tuple(row) for row in rows]


def _support_table_sql(engine):
    with engine.connect() as connection:
        return connection.execute(text(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'support_ticket'"
        )).scalar_one()


def _support_ticket_ddl(
    *,
    id_not_null=True,
    inline_id_primary_key=False,
    primary_key=True,
    title_length=120,
    content_not_null=True,
    with_fk=True,
    category_check="category IN ('BUG', 'TRADE', 'ACCOUNT', 'OTHER')",
    status_check="status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED')",
    extra_checks=(),
):
    id_constraints = []
    if id_not_null:
        id_constraints.append('NOT NULL')
    if inline_id_primary_key:
        id_constraints.append('PRIMARY KEY')
    parts = [
        f'id VARCHAR(36) {" ".join(id_constraints)}'.strip(),
        'user_id VARCHAR(36) NOT NULL',
        'category VARCHAR(20) NOT NULL',
        f'title VARCHAR({title_length}) NOT NULL',
        f'content VARCHAR(2000) {"NOT NULL" if content_not_null else "NULL"}',
        'status VARCHAR(20) NOT NULL',
        'admin_response VARCHAR(2000) NULL',
        'created_at DATETIME NOT NULL',
        'updated_at DATETIME NOT NULL',
    ]
    if primary_key is True:
        parts.append('PRIMARY KEY (id)')
    elif primary_key:
        parts.append(f'PRIMARY KEY ({primary_key})')
    if category_check is not None:
        parts.append(f'CONSTRAINT ck_support_ticket_category CHECK ({category_check})')
    if status_check is not None:
        parts.append(f'CONSTRAINT ck_support_ticket_status CHECK ({status_check})')
    for index, check in enumerate(extra_checks):
        parts.append(f'CONSTRAINT ck_support_ticket_extra_{index} CHECK ({check})')
    if with_fk:
        parts.append('FOREIGN KEY(user_id) REFERENCES "user" (id)')
    return ', '.join(parts)


def _create_malformed_support_table(
    engine,
    ddl,
    *,
    indexes='valid',
    row_category='BUG',
    row_status='OPEN',
):
    with engine.begin() as connection:
        connection.execute(text(f'CREATE TABLE support_ticket ({ddl})'))
        if indexes == 'valid':
            connection.execute(text('CREATE INDEX ix_support_ticket_user_id ON support_ticket (user_id)'))
            connection.execute(text('CREATE INDEX ix_support_ticket_status ON support_ticket (status)'))
            connection.execute(text('CREATE INDEX ix_support_ticket_created_at ON support_ticket (created_at)'))
        elif indexes == 'wrong_column':
            connection.execute(text('CREATE INDEX ix_support_ticket_user_id ON support_ticket (status)'))
            connection.execute(text('CREATE INDEX ix_support_ticket_status ON support_ticket (user_id)'))
            connection.execute(text('CREATE INDEX ix_support_ticket_created_at ON support_ticket (created_at)'))
        elif indexes == 'composite':
            connection.execute(text('CREATE INDEX ix_support_ticket_user_id ON support_ticket (user_id, status)'))
            connection.execute(text('CREATE INDEX ix_support_ticket_status ON support_ticket (status)'))
            connection.execute(text('CREATE INDEX ix_support_ticket_created_at ON support_ticket (created_at)'))
        connection.execute(text(
            "INSERT INTO support_ticket "
            "(id, user_id, category, title, content, status, admin_response, created_at, updated_at) "
            "VALUES ('ticket1', 'user1', :category, 'Preserve', 'Content', :status, NULL, "
            "'2026-01-02 00:00:00', '2026-01-02 00:00:00')"
        ), {'category': row_category, 'status': row_status})


def test_support_ticket_migration_creates_table_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'support.sqlite'}")
    _migration_base_tables(engine)

    first = migrate_support_tickets_schema(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO support_ticket "
            "(id, user_id, category, title, content, status, admin_response, created_at, updated_at) "
            "VALUES ('ticket1', 'user1', 'BUG', 'Existing', 'Content', 'OPEN', NULL, "
            "'2026-01-02 00:00:00', '2026-01-02 00:00:00')"
        ))
    before_rows = _support_rows(engine)
    second = migrate_support_tickets_schema(engine)

    assert first == 0
    assert second == 0
    assert support_ticket_table_is_compatible(engine) is True
    assert _support_rows(engine) == before_rows
    assert _unrelated_rows(engine) == [(1, 'preserve')]
    with engine.connect() as connection:
        assert connection.execute(text('PRAGMA foreign_key_check')).all() == []


def test_support_ticket_migration_accepts_exact_model_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'exact_support.sqlite'}")
    _migration_base_tables(engine)
    SupportTicket.__table__.create(bind=engine)

    assert support_ticket_table_is_compatible(engine) is True
    assert migrate_support_tickets_schema(engine) == 0


@pytest.mark.parametrize('ddl', [
    _support_ticket_ddl(primary_key=False),
    _support_ticket_ddl(content_not_null=False),
    _support_ticket_ddl(title_length=80),
    _support_ticket_ddl(with_fk=False),
    _support_ticket_ddl(category_check=None),
    _support_ticket_ddl(status_check=None),
])
def test_support_ticket_migration_rejects_incompatible_same_named_table(tmp_path, ddl):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_support.sqlite'}")
    _migration_base_tables(engine)
    _create_malformed_support_table(engine, ddl)
    before_rows = _support_rows(engine)
    before_unrelated = _unrelated_rows(engine)

    result = migrate_support_tickets_schema(engine)

    assert result == 2
    assert _support_rows(engine) == before_rows
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('ddl', [
    _support_ticket_ddl(primary_key='id, user_id'),
    _support_ticket_ddl(primary_key='user_id, id'),
    _support_ticket_ddl(id_not_null=False, primary_key='id'),
    _support_ticket_ddl(id_not_null=False, inline_id_primary_key=True, primary_key=False),
])
def test_support_ticket_migration_rejects_incompatible_primary_key_definitions(tmp_path, ddl):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_support_pk.sqlite'}")
    _migration_base_tables(engine)
    _create_malformed_support_table(engine, ddl)
    before_sql = _support_table_sql(engine)
    before_rows = _support_rows(engine)
    before_unrelated = _unrelated_rows(engine)

    result = migrate_support_tickets_schema(engine)

    assert result == 2
    assert _support_table_sql(engine) == before_sql
    assert _support_rows(engine) == before_rows
    assert _unrelated_rows(engine) == before_unrelated


def test_support_ticket_migration_rejects_missing_expected_index(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_support_index.sqlite'}")
    _migration_base_tables(engine)
    _create_malformed_support_table(engine, _support_ticket_ddl(), indexes=None)
    before_rows = _support_rows(engine)

    result = migrate_support_tickets_schema(engine)

    assert result == 2
    assert _support_rows(engine) == before_rows


@pytest.mark.parametrize('ddl,row_category,row_status', [
    (
        _support_ticket_ddl(
            category_check="category IN ('BUG', 'TRADE', 'ACCOUNT', 'OTHER', 'OPEN', 'IN_PROGRESS', 'RESOLVED')",
            status_check=None,
        ),
        'BUG',
        'OPEN',
    ),
    (_support_ticket_ddl(category_check="category IN ('BUG', 'TRADE', 'ACCOUNT', 'OTHER', 'INVALID')"), 'BUG', 'OPEN'),
    (_support_ticket_ddl(status_check="status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'INVALID')"), 'BUG', 'OPEN'),
    (
        _support_ticket_ddl(
            category_check="status IN ('BUG', 'TRADE', 'ACCOUNT', 'OTHER')",
            status_check="category IN ('OPEN', 'IN_PROGRESS', 'RESOLVED')",
        ),
        'OPEN',
        'BUG',
    ),
    (_support_ticket_ddl(status_check="status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED') OR 1 = 1"), 'BUG', 'OPEN'),
    (_support_ticket_ddl(status_check="1 = 1 OR status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED')"), 'BUG', 'OPEN'),
    (_support_ticket_ddl(status_check="NOT status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED')"), 'BUG', 'INVALID'),
    (_support_ticket_ddl(status_check="status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED') AND status != 'OPEN'"), 'BUG', 'RESOLVED'),
    (_support_ticket_ddl(category_check="category IN ('BUG', 'TRADE', 'ACCOUNT', 'OTHER') OR category = 'INVALID'"), 'BUG', 'OPEN'),
    (_support_ticket_ddl(status_check="status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED') IS TRUE"), 'BUG', 'OPEN'),
    (_support_ticket_ddl(status_check="status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED') COLLATE BINARY"), 'BUG', 'OPEN'),
    (_support_ticket_ddl(status_check="(status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED')) OR 1 = 1"), 'BUG', 'OPEN'),
])
def test_support_ticket_migration_rejects_malformed_check_constraints(tmp_path, ddl, row_category, row_status):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad_support_checks.sqlite'}")
    _migration_base_tables(engine)
    _create_malformed_support_table(engine, ddl, row_category=row_category, row_status=row_status)
    before_rows = _support_rows(engine)
    before_unrelated = _unrelated_rows(engine)

    result = migrate_support_tickets_schema(engine)

    assert result == 2
    assert _support_rows(engine) == before_rows
    assert _unrelated_rows(engine) == before_unrelated


@pytest.mark.parametrize('extra_check,row_category,row_status', [
    ("category != 'BUG'", 'OTHER', 'OPEN'),
    ("category = 'OTHER'", 'OTHER', 'OPEN'),
    ("status != 'OPEN'", 'BUG', 'RESOLVED'),
    ("status IS NOT NULL", 'BUG', 'OPEN'),
    ("length(status) > 0", 'BUG', 'OPEN'),
    ("status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED')", 'BUG', 'OPEN'),
    ("status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED') AND status != 'OPEN'", 'BUG', 'RESOLVED'),
])
def test_support_ticket_migration_rejects_additional_relevant_check_constraints(
    tmp_path,
    extra_check,
    row_category,
    row_status,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'extra_support_checks.sqlite'}")
    _migration_base_tables(engine)
    _create_malformed_support_table(
        engine,
        _support_ticket_ddl(extra_checks=(extra_check,)),
        row_category=row_category,
        row_status=row_status,
    )
    before_sql = _support_table_sql(engine)
    before_rows = _support_rows(engine)
    before_unrelated = _unrelated_rows(engine)

    result = migrate_support_tickets_schema(engine)

    assert result == 2
    assert _support_table_sql(engine) == before_sql
    assert _support_rows(engine) == before_rows
    assert _unrelated_rows(engine) == before_unrelated


def test_support_ticket_migration_ignores_column_name_inside_string_literal(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'literal_status_support_checks.sqlite'}")
    _migration_base_tables(engine)
    _create_malformed_support_table(
        engine,
        _support_ticket_ddl(extra_checks=("title != 'status'",)),
    )
    before_rows = _support_rows(engine)
    before_unrelated = _unrelated_rows(engine)

    assert support_ticket_table_is_compatible(engine) is True
    assert migrate_support_tickets_schema(engine) == 0
    assert _support_rows(engine) == before_rows
    assert _unrelated_rows(engine) == before_unrelated


def test_support_ticket_check_parser_rejects_unbalanced_parentheses():
    assert _constraint_values_for_column("(status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED')", 'status') is None


def test_support_ticket_migration_accepts_harmless_check_formatting_differences(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'formatted_support_checks.sqlite'}")
    _migration_base_tables(engine)
    _create_malformed_support_table(
        engine,
        _support_ticket_ddl(
            category_check='( "category" IN ( "OTHER" , "ACCOUNT" , "TRADE" , "BUG" ) )',
            status_check="\n status   IN   ( 'RESOLVED' , 'OPEN' , 'IN_PROGRESS' ) ",
        ),
    )

    assert support_ticket_table_is_compatible(engine) is True
    assert migrate_support_tickets_schema(engine) == 0


def test_support_ticket_migration_accepts_nested_outer_parentheses_and_quote_differences(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'nested_formatted_support_checks.sqlite'}")
    _migration_base_tables(engine)
    _create_malformed_support_table(
        engine,
        _support_ticket_ddl(
            category_check='(((category IN ("BUG","OTHER","TRADE","ACCOUNT"))))',
            status_check='((("status" IN ("IN_PROGRESS", "RESOLVED", "OPEN"))))',
        ),
    )

    assert support_ticket_table_is_compatible(engine) is True
    assert migrate_support_tickets_schema(engine) == 0


@pytest.mark.parametrize('index_mode', ['wrong_column', 'composite'])
def test_support_ticket_migration_rejects_wrong_expected_index_mapping(tmp_path, index_mode):
    engine = create_engine(f"sqlite:///{tmp_path / f'bad_support_{index_mode}.sqlite'}")
    _migration_base_tables(engine)
    _create_malformed_support_table(engine, _support_ticket_ddl(), indexes=index_mode)
    before_rows = _support_rows(engine)
    before_unrelated = _unrelated_rows(engine)

    result = migrate_support_tickets_schema(engine)

    assert result == 2
    assert _support_rows(engine) == before_rows
    assert _unrelated_rows(engine) == before_unrelated


def test_support_ticket_migration_stops_for_unsupported_database():
    class FakeDialect:
        name = 'postgresql'

    class FakeEngine:
        dialect = FakeDialect()

    assert migrate_support_tickets_schema(FakeEngine()) == 2
    assert support_ticket_table_is_compatible(FakeEngine()) is False
