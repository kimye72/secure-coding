from flask import session

def test_login_get(client):
    response = client.get('/login')
    assert response.status_code == 200

def test_login_valid(client, normal_user):
    response = client.post('/login', data={'username': 'testuser', 'password': 'TestPassword123!'})
    assert response.status_code == 302
    assert response.headers['Location'] == '/dashboard'
    with client.session_transaction() as sess:
        assert sess.get('user_id') == normal_user

def test_login_invalid(client, normal_user):
    response = client.post('/login', data={'username': 'testuser', 'password': 'wrongpassword'})
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/login') or response.headers['Location'] == '/login'

    response2 = client.get('/login')
    body = response2.get_data(as_text=True)
    assert '아이디 또는 비밀번호가 올바르지 않습니다.' in body
    with client.session_transaction() as sess:
        assert 'user_id' not in sess

def test_login_clears_stale_session(client, normal_user):
    with client.session_transaction() as sess:
        sess['stale_data'] = 'should_be_cleared'

    response = client.post('/login', data={'username': 'testuser', 'password': 'TestPassword123!'})
    assert response.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get('user_id') == normal_user
        assert 'stale_data' not in sess

def test_logout_get(client):
    response = client.get('/logout')
    assert response.status_code == 405

def test_logout_post(client, auth_helper, normal_user):
    response = auth_helper.login('testuser')
    assert response.status_code == 302

    with client.session_transaction() as sess:
        assert 'user_id' in sess

    response = client.post('/logout')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/login')

    with client.session_transaction() as sess:
        assert 'user_id' not in sess