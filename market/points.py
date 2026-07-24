from datetime import datetime

from sqlalchemy.exc import IntegrityError

from market.extensions import db


MAX_POINT_BALANCE = 1_000_000_000
MAX_ADMIN_POINT_GRANT = 1_000_000


class PointError(Exception):
    pass


def parse_positive_int(value, maximum=None):
    try:
        amount = int((value or '').strip())
    except (TypeError, ValueError):
        raise PointError()
    if str(value).strip() != str(amount):
        raise PointError()
    if amount <= 0:
        raise PointError()
    if maximum is not None and amount > maximum:
        raise PointError()
    return amount


def authoritative_trade_amount(trade):
    product = trade.product
    if product is None:
        raise PointError()
    amount = product.price
    if not isinstance(amount, int) or amount <= 0 or amount > MAX_POINT_BALANCE:
        raise PointError()
    return amount


def ensure_wallet(user_id):
    from market.models import PointWallet

    wallet = PointWallet.query.filter_by(user_id=user_id).first()
    if wallet is not None:
        return wallet

    wallet = PointWallet(user_id=user_id, balance=0)
    db.session.add(wallet)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        raise
    return wallet


def credit_wallet(user_id, amount):
    from market.models import PointWallet

    ensure_wallet(user_id)
    result = db.session.execute(
        db.update(PointWallet)
        .where(
            PointWallet.user_id == user_id,
            PointWallet.balance <= MAX_POINT_BALANCE - amount,
        )
        .values(
            balance=PointWallet.balance + amount,
            updated_at=datetime.utcnow(),
        )
    )
    return result.rowcount == 1


def debit_wallet(user_id, amount):
    from market.models import PointWallet

    ensure_wallet(user_id)
    result = db.session.execute(
        db.update(PointWallet)
        .where(
            PointWallet.user_id == user_id,
            PointWallet.balance >= amount,
        )
        .values(
            balance=PointWallet.balance - amount,
            updated_at=datetime.utcnow(),
        )
    )
    return result.rowcount == 1


def ledger_label(entry_type):
    from market.models import PointLedger

    labels = {
        PointLedger.TYPE_ADMIN_GRANT: '관리자 테스트 포인트 지급',
        PointLedger.TYPE_ESCROW_DEBIT: '거래 대금 에스크로 차감',
        PointLedger.TYPE_ESCROW_REFUND: '거래 취소 환불',
        PointLedger.TYPE_SETTLEMENT_CREDIT: '거래 완료 정산',
    }
    return labels.get(entry_type, '포인트 내역')
