"""
SQLAlchemy 모델 정의 (market/models.py)
- Python 3.9 호환 classic Flask-SQLAlchemy 문법 사용
- db.Model, db.Column, db.relationship, db.CheckConstraint
- 이 파일 안에서 request, session, flash, redirect, template, SocketIO를 사용하지 않음
"""
import uuid
from datetime import datetime

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

    __table_args__ = (
        db.CheckConstraint("role IN ('USER', 'ADMIN')", name='ck_user_role'),
    )

    def __repr__(self):
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

    __table_args__ = (
        db.CheckConstraint('buyer_id != seller_id', name='ck_trade_buyer_ne_seller'),
        db.CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'REJECTED', 'CANCELLED')",
            name='ck_trade_status',
        ),
    )

    def __repr__(self):
        return f'<Trade id={self.id} status={self.status}>'


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
