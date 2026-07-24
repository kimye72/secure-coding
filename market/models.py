"""
SQLAlchemy 모델 정의 (market/models.py)
- Python 3.9 호환 classic Flask-SQLAlchemy 문법 사용
- db.Model, db.Column, db.relationship, db.CheckConstraint
- 이 파일 안에서 request, session, flash, redirect, template, SocketIO를 사용하지 않음
"""
import uuid
from datetime import datetime

from werkzeug.security import generate_password_hash, check_password_hash

from market.extensions import db


def generate_uuid():
    """기본 키 생성용 UUID 함수"""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class User(db.Model):
    __tablename__ = 'user'

    ROLE_USER = 'USER'
    ROLE_ADMIN = 'ADMIN'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    username = db.Column(db.String(20), unique=True, index=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    bio = db.Column(db.String(300), nullable=True)
    role = db.Column(db.String(20), nullable=False, default='USER')
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # 관계
    products = db.relationship('Product', back_populates='seller', foreign_keys='Product.seller_id')
    purchase_trades = db.relationship('Trade', back_populates='buyer', foreign_keys='Trade.buyer_id')
    sale_trades = db.relationship('Trade', back_populates='seller', foreign_keys='Trade.seller_id')
    submitted_reports = db.relationship('Report', back_populates='reporter', foreign_keys='Report.reporter_id')
    handled_reports = db.relationship('Report', back_populates='handler', foreign_keys='Report.handled_by')
    written_reviews = db.relationship('Review', back_populates='reviewer', foreign_keys='Review.reviewer_id')
    received_reviews = db.relationship('Review', back_populates='reviewee', foreign_keys='Review.reviewee_id')
    keyword_subscriptions = db.relationship('KeywordSubscription', back_populates='user', foreign_keys='KeywordSubscription.user_id')
    notifications = db.relationship('Notification', back_populates='user', foreign_keys='Notification.user_id')
    support_tickets = db.relationship('SupportTicket', back_populates='user', foreign_keys='SupportTicket.user_id')

    __table_args__ = (
        db.CheckConstraint("role IN ('USER', 'ADMIN')", name='ck_user_role'),
    )

    def set_password(self, password):
        """Werkzeug로 비밀번호를 해시하여 저장한다. 평문은 저장하지 않는다."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """제공된 평문 비밀번호가 저장된 해시와 일치하는지 확인한다."""
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        # password_hash는 의도적으로 포함하지 않음
        return f'<User id={self.id} username={self.username} role={self.role}>'



# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------

class Product(db.Model):
    __tablename__ = 'product'

    STATUS_SELLING = 'SELLING'
    STATUS_RESERVED = 'RESERVED'
    STATUS_SOLD = 'SOLD'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    seller_id = db.Column(db.String(36), db.ForeignKey('user.id'), index=True, nullable=False)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False)
    price = db.Column(db.Integer, nullable=False)
    trade_location = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='SELLING')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    # 관계 (seller를 삭제해도 상품이 삭제되지 않음, cascade 없음)
    seller = db.relationship('User', back_populates='products', foreign_keys=[seller_id])
    trades = db.relationship('Trade', back_populates='product', foreign_keys='Trade.product_id')
    reviews = db.relationship('Review', back_populates='product', foreign_keys='Review.product_id')
    notifications = db.relationship('Notification', back_populates='product', foreign_keys='Notification.product_id')

    __table_args__ = (
        db.CheckConstraint('price > 0', name='ck_product_price_positive'),
        db.CheckConstraint(
            "status IN ('SELLING', 'RESERVED', 'SOLD')",
            name='ck_product_status',
        ),
    )

    def __repr__(self):
        return f'<Product id={self.id} title={self.title} status={self.status}>'


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------

class Trade(db.Model):
    __tablename__ = 'trade'

    STATUS_PENDING = 'PENDING'
    STATUS_ACCEPTED = 'ACCEPTED'
    STATUS_REJECTED = 'REJECTED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_COMPLETED = 'COMPLETED'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    product_id = db.Column(db.String(36), db.ForeignKey('product.id'), index=True, nullable=False)
    buyer_id = db.Column(db.String(36), db.ForeignKey('user.id'), index=True, nullable=False)
    seller_id = db.Column(db.String(36), db.ForeignKey('user.id'), index=True, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='PENDING')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    # 관계 (foreign_keys 명시)
    product = db.relationship('Product', back_populates='trades', foreign_keys=[product_id])
    buyer = db.relationship('User', back_populates='purchase_trades', foreign_keys=[buyer_id])
    seller = db.relationship('User', back_populates='sale_trades', foreign_keys=[seller_id])
    reviews = db.relationship('Review', back_populates='trade', foreign_keys='Review.trade_id')

    __table_args__ = (
        db.CheckConstraint('buyer_id != seller_id', name='ck_trade_buyer_ne_seller'),
        db.CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'REJECTED', 'CANCELLED', 'COMPLETED')",
            name='ck_trade_status',
        ),
    )

    def __repr__(self):
        return f'<Trade id={self.id} status={self.status}>'


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

class Review(db.Model):
    __tablename__ = 'review'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    trade_id = db.Column(db.String(36), db.ForeignKey('trade.id'), index=True, nullable=False)
    product_id = db.Column(db.String(36), db.ForeignKey('product.id'), index=True, nullable=False)
    reviewer_id = db.Column(db.String(36), db.ForeignKey('user.id'), index=True, nullable=False)
    reviewee_id = db.Column(db.String(36), db.ForeignKey('user.id'), index=True, nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    content = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    trade = db.relationship('Trade', back_populates='reviews', foreign_keys=[trade_id])
    product = db.relationship('Product', back_populates='reviews', foreign_keys=[product_id])
    reviewer = db.relationship('User', back_populates='written_reviews', foreign_keys=[reviewer_id])
    reviewee = db.relationship('User', back_populates='received_reviews', foreign_keys=[reviewee_id])

    __table_args__ = (
        db.CheckConstraint('rating >= 1 AND rating <= 5', name='ck_review_rating_range'),
        db.CheckConstraint('reviewer_id != reviewee_id', name='ck_review_reviewer_ne_reviewee'),
        db.UniqueConstraint('trade_id', 'reviewer_id', name='uq_review_trade_reviewer'),
    )

    def __repr__(self):
        return f'<Review id={self.id} trade_id={self.trade_id} rating={self.rating}>'


# ---------------------------------------------------------------------------
# KeywordSubscription
# ---------------------------------------------------------------------------

class KeywordSubscription(db.Model):
    __tablename__ = 'keyword_subscription'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('user.id'), index=True, nullable=False)
    keyword = db.Column(db.String(80), nullable=False)
    normalized_keyword = db.Column(db.String(240), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship('User', back_populates='keyword_subscriptions', foreign_keys=[user_id])

    __table_args__ = (
        db.UniqueConstraint('user_id', 'normalized_keyword', name='uq_keyword_subscription_user_normalized'),
    )

    def __repr__(self):
        return f'<KeywordSubscription id={self.id} user_id={self.user_id}>'


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

class Notification(db.Model):
    __tablename__ = 'notification'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('user.id'), index=True, nullable=False)
    product_id = db.Column(db.String(36), db.ForeignKey('product.id'), index=True, nullable=False)
    matched_keyword = db.Column(db.String(80), nullable=False)
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship('User', back_populates='notifications', foreign_keys=[user_id])
    product = db.relationship('Product', back_populates='notifications', foreign_keys=[product_id])

    __table_args__ = (
        db.UniqueConstraint('user_id', 'product_id', name='uq_notification_user_product'),
    )

    def __repr__(self):
        return f'<Notification id={self.id} user_id={self.user_id} is_read={self.is_read}>'


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class Report(db.Model):
    __tablename__ = 'report'

    TARGET_USER = 'USER'
    TARGET_PRODUCT = 'PRODUCT'
    STATUS_PENDING = 'PENDING'
    STATUS_RESOLVED = 'RESOLVED'
    STATUS_DISMISSED = 'DISMISSED'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    reporter_id = db.Column(db.String(36), db.ForeignKey('user.id'), index=True, nullable=False)
    target_type = db.Column(db.String(20), nullable=False)
    target_id = db.Column(db.String(36), index=True, nullable=False)  # ForeignKey 없음 (user or product)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='PENDING')
    handled_by = db.Column(db.String(36), db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    handled_at = db.Column(db.DateTime, nullable=True)

    # 관계 (foreign_keys 명시 — reporter_id와 handled_by 모두 User 참조)
    reporter = db.relationship('User', back_populates='submitted_reports', foreign_keys=[reporter_id])
    handler = db.relationship('User', back_populates='handled_reports', foreign_keys=[handled_by])

    __table_args__ = (
        db.CheckConstraint(
            "target_type IN ('USER', 'PRODUCT')",
            name='ck_report_target_type',
        ),
        db.CheckConstraint(
            "status IN ('PENDING', 'RESOLVED', 'DISMISSED')",
            name='ck_report_status',
        ),
    )

    def __repr__(self):
        return f'<Report id={self.id} target_type={self.target_type} status={self.status}>'


# ---------------------------------------------------------------------------
# SupportTicket
# ---------------------------------------------------------------------------

class SupportTicket(db.Model):
    __tablename__ = 'support_ticket'

    CATEGORY_BUG = 'BUG'
    CATEGORY_TRADE = 'TRADE'
    CATEGORY_ACCOUNT = 'ACCOUNT'
    CATEGORY_OTHER = 'OTHER'

    STATUS_OPEN = 'OPEN'
    STATUS_IN_PROGRESS = 'IN_PROGRESS'
    STATUS_RESOLVED = 'RESOLVED'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('user.id'), index=True, nullable=False)
    category = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(120), nullable=False)
    content = db.Column(db.String(2000), nullable=False)
    status = db.Column(db.String(20), index=True, nullable=False, default='OPEN')
    admin_response = db.Column(db.String(2000), nullable=True)
    created_at = db.Column(db.DateTime, index=True, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    user = db.relationship('User', back_populates='support_tickets', foreign_keys=[user_id])

    __table_args__ = (
        db.CheckConstraint(
            "category IN ('BUG', 'TRADE', 'ACCOUNT', 'OTHER')",
            name='ck_support_ticket_category',
        ),
        db.CheckConstraint(
            "status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED')",
            name='ck_support_ticket_status',
        ),
    )

    def __repr__(self):
        return f'<SupportTicket id={self.id} status={self.status}>'
