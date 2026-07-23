def test_security_headers_ordinary_response(client):
    response = client.get('/login')
    assert response.headers.get('X-Content-Type-Options') == 'nosniff'
    assert response.headers.get('X-Frame-Options') == 'DENY'
    assert response.headers.get('Referrer-Policy') == 'strict-origin-when-cross-origin'
    assert response.headers.get('Permissions-Policy') == 'camera=(), microphone=(), geolocation=()'

def test_session_cookie_config(app):
    assert app.config.get('SESSION_COOKIE_HTTPONLY') is True
    assert app.config.get('SESSION_COOKIE_SAMESITE') == 'Lax'

def test_security_headers_csrf_400(app):
    # Need to trigger a CSRF error, which requires an app with CSRF enabled
    from market import create_app
    csrf_app = create_app(test_config={
        'TESTING': True,
        'WTF_CSRF_ENABLED': True,
        'RATELIMIT_ENABLED': False,
        'SECRET_KEY': 'test'
    })
    csrf_client = csrf_app.test_client()
    response = csrf_client.post('/login')
    assert response.status_code == 400
    assert response.headers.get('X-Content-Type-Options') == 'nosniff'
    assert response.headers.get('X-Frame-Options') == 'DENY'
    assert response.headers.get('Referrer-Policy') == 'strict-origin-when-cross-origin'
    assert response.headers.get('Permissions-Policy') == 'camera=(), microphone=(), geolocation=()'

def test_security_headers_rate_limit_429(app):
    # Need to trigger a 429 error
    from market import create_app
    rate_app = create_app(test_config={
        'TESTING': True,
        'WTF_CSRF_ENABLED': False,
        'RATELIMIT_ENABLED': True,
        'RATELIMIT_STORAGE_URI': 'memory://',
        'SECRET_KEY': 'test'
    })
    rate_client = rate_app.test_client()

    # Exhaust limits
    for _ in range(6):
        response = rate_client.post('/login', data={'username': 'test', 'password': 'pwd'})

    assert response.status_code == 429
    assert response.headers.get('X-Content-Type-Options') == 'nosniff'
    assert response.headers.get('X-Frame-Options') == 'DENY'
    assert response.headers.get('Referrer-Policy') == 'strict-origin-when-cross-origin'
    assert response.headers.get('Permissions-Policy') == 'camera=(), microphone=(), geolocation=()'
