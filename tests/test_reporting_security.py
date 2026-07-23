from market.models import Report, User, Product
from market.extensions import db
import pytest

def test_user_cannot_report_self(client, auth_helper, normal_user):
    auth_helper.login('testuser')
    response = client.post('/report', data={
        'target_type': Report.TARGET_USER,
        'target_id': normal_user,
        'reason': 'Spam'
    })
    # Might redirect to /report with flash or /
    assert response.status_code == 302

def test_product_owner_cannot_report_own_product(client, auth_helper, product):
    auth_helper.login('testuser')
    response = client.post('/report', data={
        'target_type': Report.TARGET_PRODUCT,
        'target_id': product,
        'reason': 'Spam'
    })
    assert response.status_code == 302

def test_valid_report_and_no_duplicate(client, app, auth_helper, normal_user, second_user):
    auth_helper.login('testuser')
    # Valid report
    response = client.post('/report', data={
        'target_type': Report.TARGET_USER,
        'target_id': second_user,
        'reason': 'Spam'
    })
    assert response.status_code == 302

    with app.app_context():
        reports = db.session.query(Report).filter_by(reporter_id=normal_user, target_id=second_user).all()
        assert len(reports) == 1

    # Duplicate report
    response2 = client.post('/report', data={
        'target_type': Report.TARGET_USER,
        'target_id': second_user,
        'reason': 'More Spam'
    })
    assert response2.status_code == 302
    with app.app_context():
        reports = db.session.query(Report).filter_by(reporter_id=normal_user, target_id=second_user).all()
        assert len(reports) == 1 # Still 1

def test_normal_user_cannot_resolve_reject(client, app, auth_helper, normal_user, second_user):
    with app.app_context():
        r = Report(reporter_id=normal_user, target_type=Report.TARGET_USER, target_id=second_user, reason='Spam', status=Report.STATUS_PENDING)
        db.session.add(r)
        db.session.commit()
        report_id = r.id

    auth_helper.login('seconduser')
    response1 = client.post(f'/admin/report/{report_id}/resolve')
    assert response1.status_code == 302

    response2 = client.post(f'/admin/report/{report_id}/reject')
    assert response2.status_code == 302

    with app.app_context():
        r = db.session.get(Report, report_id)
        assert r.status == Report.STATUS_PENDING

def test_admin_can_resolve(client, app, auth_helper, normal_user, second_user, admin_user):
    with app.app_context():
        r = Report(reporter_id=normal_user, target_type=Report.TARGET_USER, target_id=second_user, reason='Spam', status=Report.STATUS_PENDING)
        db.session.add(r)
        db.session.commit()
        report_id = r.id

    auth_helper.login('adminuser')
    response = client.post(f'/admin/report/{report_id}/resolve')
    assert response.status_code == 302

    with app.app_context():
        r = db.session.get(Report, report_id)
        assert r.status == Report.STATUS_RESOLVED
        assert r.handled_by == admin_user

        # Test no automatic suspension (is_active is True for the target)
        u = db.session.get(User, second_user)
        assert u.is_active is True

def test_admin_can_reject_dismiss(client, app, auth_helper, normal_user, second_user, admin_user):
    with app.app_context():
        r = Report(reporter_id=normal_user, target_type=Report.TARGET_USER, target_id=second_user, reason='Spam', status=Report.STATUS_PENDING)
        db.session.add(r)
        db.session.commit()
        report_id = r.id

    auth_helper.login('adminuser')
    response = client.post(f'/admin/report/{report_id}/reject') # or whatever the route is
    assert response.status_code == 302

    with app.app_context():
        r = db.session.get(Report, report_id)
        assert r.status == Report.STATUS_DISMISSED
        assert r.handled_by == admin_user

def test_completed_report_cannot_be_transitioned(client, app, auth_helper, normal_user, second_user, admin_user):
    with app.app_context():
        r = Report(reporter_id=normal_user, target_type=Report.TARGET_USER, target_id=second_user, reason='Spam', status=Report.STATUS_RESOLVED, handled_by=admin_user)
        db.session.add(r)
        db.session.commit()
        report_id = r.id

    auth_helper.login('adminuser')
    response = client.post(f'/admin/report/{report_id}/reject')
    assert response.status_code == 302

    with app.app_context():
        r = db.session.get(Report, report_id)
        assert r.status == Report.STATUS_RESOLVED # Still RESOLVED
