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
from market.points import MAX_POINT_BALANCE


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
    point_wallet = db.relationship('PointWallet', back_populates='user', foreign_keys='PointWallet.user_id', uselist=False)
    point_ledger_entries = db.relationship('PointLedger', back_populates='user', foreign_keys='PointLedger.user_id')

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
    payment = db.relationship('TradePayment', back_populates='trade', foreign_keys='TradePayment.trade_id', uselist=False)

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
# Point wallet / escrow payment / ledger
# ---------------------------------------------------------------------------

class PointWallet(db.Model):
    __tablename__ = 'point_wallet'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('user.id'), unique=True, index=True, nullable=False)
    balance = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    user = db.relationship('User', back_populates='point_wallet', foreign_keys=[user_id])

    __table_args__ = (
        db.CheckConstraint(
            f'balance >= 0 AND balance <= {MAX_POINT_BALANCE}',
            name='ck_point_wallet_balance_range',
        ),
    )

    def __repr__(self):
        return f'<PointWallet id={self.id} user_id={self.user_id}>'


class TradePayment(db.Model):
    __tablename__ = 'trade_payment'

    STATUS_HELD = 'HELD'
    STATUS_SETTLED = 'SETTLED'
    STATUS_REFUNDED = 'REFUNDED'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    trade_id = db.Column(db.String(36), db.ForeignKey('trade.id'), unique=True, index=True, nullable=False)
    buyer_id = db.Column(db.String(36), db.ForeignKey('user.id'), index=True, nullable=False)
    seller_id = db.Column(db.String(36), db.ForeignKey('user.id'), index=True, nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), index=True, nullable=False, default='HELD')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    settled_at = db.Column(db.DateTime, nullable=True)
    refunded_at = db.Column(db.DateTime, nullable=True)

    trade = db.relationship('Trade', back_populates='payment', foreign_keys=[trade_id])
    buyer = db.relationship('User', foreign_keys=[buyer_id])
    seller = db.relationship('User', foreign_keys=[seller_id])
    ledger_entries = db.relationship('PointLedger', back_populates='payment', foreign_keys='PointLedger.payment_id')

    __table_args__ = (
        db.CheckConstraint(
            f'amount >= 1 AND amount <= {MAX_POINT_BALANCE}',
            name='ck_trade_payment_amount_range',
        ),
        db.CheckConstraint('buyer_id != seller_id', name='ck_trade_payment_buyer_ne_seller'),
        db.CheckConstraint(
            "status IN ('HELD', 'SETTLED', 'REFUNDED')",
            name='ck_trade_payment_status',
        ),
        db.CheckConstraint(
            "("
            "status = 'HELD' AND settled_at IS NULL AND refunded_at IS NULL"
            ") OR ("
            "status = 'SETTLED' AND settled_at IS NOT NULL AND refunded_at IS NULL"
            ") OR ("
            "status = 'REFUNDED' AND refunded_at IS NOT NULL AND settled_at IS NULL"
            ")",
            name='ck_trade_payment_status_timestamps',
        ),
    )

    def __repr__(self):
        return f'<TradePayment id={self.id} trade_id={self.trade_id} status={self.status}>'


class PointLedger(db.Model):
    __tablename__ = 'point_ledger'

    TYPE_ADMIN_GRANT = 'ADMIN_GRANT'
    TYPE_ESCROW_DEBIT = 'ESCROW_DEBIT'
    TYPE_ESCROW_REFUND = 'ESCROW_REFUND'
    TYPE_SETTLEMENT_CREDIT = 'SETTLEMENT_CREDIT'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('user.id'), index=True, nullable=False)
    payment_id = db.Column(db.String(36), db.ForeignKey('trade_payment.id'), index=True, nullable=True)
    trade_id = db.Column(db.String(36), db.ForeignKey('trade.id'), index=True, nullable=True)
    entry_type = db.Column(db.String(30), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, index=True, nullable=False, default=datetime.utcnow)

    user = db.relationship('User', back_populates='point_ledger_entries', foreign_keys=[user_id])
    payment = db.relationship('TradePayment', back_populates='ledger_entries', foreign_keys=[payment_id])
    trade = db.relationship('Trade', foreign_keys=[trade_id])

    __table_args__ = (
        db.CheckConstraint(
            f'amount >= 1 AND amount <= {MAX_POINT_BALANCE}',
            name='ck_point_ledger_amount_range',
        ),
        db.CheckConstraint(
            "entry_type IN ('ADMIN_GRANT', 'ESCROW_DEBIT', 'ESCROW_REFUND', 'SETTLEMENT_CREDIT')",
            name='ck_point_ledger_entry_type',
        ),
        db.CheckConstraint(
            "("
            "entry_type = 'ADMIN_GRANT' AND payment_id IS NULL AND trade_id IS NULL"
            ") OR ("
            "entry_type IN ('ESCROW_DEBIT', 'ESCROW_REFUND', 'SETTLEMENT_CREDIT') "
            "AND payment_id IS NOT NULL AND trade_id IS NOT NULL"
            ")",
            name='ck_point_ledger_reference_consistency',
        ),
        db.UniqueConstraint('payment_id', 'user_id', 'entry_type', name='uq_point_ledger_payment_user_type'),
    )

    def __repr__(self):
        return f'<PointLedger id={self.id} user_id={self.user_id} entry_type={self.entry_type}>'


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
