import sys

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from market.points import MAX_POINT_BALANCE


PAYMENT_TABLES = ('point_wallet', 'trade_payment', 'point_ledger')

EXPECTED_COLUMNS = {
    'point_wallet': {
        'id', 'user_id', 'balance', 'created_at', 'updated_at',
    },
    'trade_payment': {
        'id', 'trade_id', 'buyer_id', 'seller_id', 'amount', 'status',
        'created_at', 'updated_at', 'settled_at', 'refunded_at',
    },
    'point_ledger': {
        'id', 'user_id', 'payment_id', 'trade_id', 'entry_type', 'amount', 'created_at',
    },
}

EXPECTED_LENGTHS = {
    'point_wallet': {'id': 36, 'user_id': 36},
    'trade_payment': {
        'id': 36, 'trade_id': 36, 'buyer_id': 36, 'seller_id': 36, 'status': 20,
    },
    'point_ledger': {
        'id': 36, 'user_id': 36, 'payment_id': 36, 'trade_id': 36, 'entry_type': 30,
    },
}

EXPECTED_NONNULL = {
    'point_wallet': {'id', 'user_id', 'balance', 'created_at', 'updated_at'},
    'trade_payment': {
        'id', 'trade_id', 'buyer_id', 'seller_id', 'amount', 'status', 'created_at', 'updated_at',
    },
    'point_ledger': {'id', 'user_id', 'entry_type', 'amount', 'created_at'},
}

EXPECTED_FKS = {
    'point_wallet': {('user_id', 'user', 'id')},
    'trade_payment': {
        ('trade_id', 'trade', 'id'),
        ('buyer_id', 'user', 'id'),
        ('seller_id', 'user', 'id'),
    },
    'point_ledger': {
        ('user_id', 'user', 'id'),
        ('payment_id', 'trade_payment', 'id'),
        ('trade_id', 'trade', 'id'),
    },
}

EXPECTED_FK_OPTIONS = {
    'ondelete': None,
    'onupdate': None,
    'deferrable': None,
    'initially': None,
}

EXPECTED_UNIQUES = {
    'point_wallet': {('user_id',)},
    'trade_payment': {('trade_id',)},
    'point_ledger': {('payment_id', 'user_id', 'entry_type')},
}

EXPECTED_INDEXES = {
    'point_wallet': {
        'ix_point_wallet_user_id': (('user_id',), True),
    },
    'trade_payment': {
        'ix_trade_payment_buyer_id': (('buyer_id',), False),
        'ix_trade_payment_seller_id': (('seller_id',), False),
        'ix_trade_payment_status': (('status',), False),
        'ix_trade_payment_trade_id': (('trade_id',), True),
    },
    'point_ledger': {
        'ix_point_ledger_created_at': (('created_at',), False),
        'ix_point_ledger_payment_id': (('payment_id',), False),
        'ix_point_ledger_trade_id': (('trade_id',), False),
        'ix_point_ledger_user_id': (('user_id',), False),
    },
}

EXPECTED_DATETIME_COLUMNS = {
    'point_wallet': {'created_at', 'updated_at'},
    'trade_payment': {'created_at', 'updated_at', 'settled_at', 'refunded_at'},
    'point_ledger': {'created_at'},
}

EXPECTED_CHECKS = {
    'point_wallet': {
        f'balance >= 0 AND balance <= {MAX_POINT_BALANCE}',
    },
    'trade_payment': {
        f'amount >= 1 AND amount <= {MAX_POINT_BALANCE}',
        'buyer_id != seller_id',
        "status IN ('HELD', 'SETTLED', 'REFUNDED')",
        (
            "(status = 'HELD' AND settled_at IS NULL AND refunded_at IS NULL) "
            "OR (status = 'SETTLED' AND settled_at IS NOT NULL AND refunded_at IS NULL) "
            "OR (status = 'REFUNDED' AND refunded_at IS NOT NULL AND settled_at IS NULL)"
        ),
    },
    'point_ledger': {
        f'amount >= 1 AND amount <= {MAX_POINT_BALANCE}',
        "entry_type IN ('ADMIN_GRANT', 'ESCROW_DEBIT', 'ESCROW_REFUND', 'SETTLEMENT_CREDIT')",
        (
            "(entry_type = 'ADMIN_GRANT' AND payment_id IS NULL AND trade_id IS NULL) "
            "OR (entry_type IN ('ESCROW_DEBIT', 'ESCROW_REFUND', 'SETTLEMENT_CREDIT') "
            "AND payment_id IS NOT NULL AND trade_id IS NOT NULL)"
        ),
    },
}

def _table_names(engine):
    return set(inspect(engine).get_table_names())


def _column_map(engine, table_name):
    return {column['name']: column for column in inspect(engine).get_columns(table_name)}


def _column_length(column):
    return getattr(column['type'], 'length', None)


def _has_exact_primary_key(columns):
    if columns['id'].get('primary_key') != 1:
        return False
    if columns['id'].get('nullable'):
        return False
    for column_name, column in columns.items():
        if column_name != 'id' and column.get('primary_key'):
            return False
    return True


def _type_name(column):
    return column['type'].__class__.__name__.upper()


def _is_integer_column(column):
    return _type_name(column) == 'INTEGER'


def _is_datetime_column(column):
    return _type_name(column) == 'DATETIME'


def _is_varchar_column(column):
    return _type_name(column) == 'VARCHAR'


def _normalize_fk_action(value):
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if normalized == 'NO ACTION':
        return None
    return normalized


def _normalize_fk_options(options):
    options = options or {}
    normalized = {}
    for key in EXPECTED_FK_OPTIONS:
        normalized[key] = _normalize_fk_action(options.get(key))
    unexpected = set(options) - set(EXPECTED_FK_OPTIONS)
    if unexpected:
        return None
    return normalized


def _actual_fks(engine, table_name):
    foreign_keys = inspect(engine).get_foreign_keys(table_name)
    actual = []
    for fk in foreign_keys:
        if len(fk.get('constrained_columns', [])) != 1 or len(fk.get('referred_columns', [])) != 1:
            return None
        if fk.get('referred_schema') is not None:
            return None
        options = _normalize_fk_options(fk.get('options') or {})
        if options != EXPECTED_FK_OPTIONS:
            return None
        actual.append((fk['constrained_columns'][0], fk['referred_table'], fk['referred_columns'][0]))
    return actual


def _quote_identifier(identifier):
    return '"' + str(identifier).replace('"', '""') + '"'


def _row_value(row, key, index):
    mapping = row._mapping
    if key in mapping:
        return mapping[key]
    return row[index]


def _raw_sqlite_indexes(engine, table_name):
    indexes = []
    with engine.connect() as connection:
        for index_row in connection.exec_driver_sql(f'PRAGMA index_list({_quote_identifier(table_name)})'):
            index_name = _row_value(index_row, 'name', 1)
            key_columns = []
            has_expression = False
            has_desc = False
            has_nonbinary_collation = False

            for info_row in connection.exec_driver_sql(f'PRAGMA index_xinfo({_quote_identifier(index_name)})'):
                if not bool(_row_value(info_row, 'key', 5)):
                    continue

                cid = _row_value(info_row, 'cid', 1)
                column_name = _row_value(info_row, 'name', 2)
                if cid == -2 or column_name is None:
                    has_expression = True
                else:
                    key_columns.append(column_name)

                if bool(_row_value(info_row, 'desc', 3)):
                    has_desc = True
                if str(_row_value(info_row, 'coll', 4) or '').upper() != 'BINARY':
                    has_nonbinary_collation = True

            indexes.append({
                'name': index_name,
                'unique': bool(_row_value(index_row, 'unique', 2)),
                'origin': _row_value(index_row, 'origin', 3),
                'partial': bool(_row_value(index_row, 'partial', 4)),
                'columns': tuple(key_columns),
                'has_expression': has_expression,
                'has_desc': has_desc,
                'has_nonbinary_collation': has_nonbinary_collation,
            })
    return indexes


def _actual_uniques(engine, table_name):
    uniques = []
    for index in _raw_sqlite_indexes(engine, table_name):
        if index['origin'] == 'pk':
            continue
        if index['partial'] or index['has_expression'] or index['has_desc'] or index['has_nonbinary_collation']:
            return None
        if index['unique']:
            uniques.append(index['columns'])
    return uniques


def _has_indexes(engine, table_name):
    indexes = {index['name']: index for index in _raw_sqlite_indexes(engine, table_name)}
    for index in indexes.values():
        if index['origin'] == 'pk':
            continue
        if (
            index['partial']
            or index['has_expression']
            or index['has_desc']
            or index['has_nonbinary_collation']
        ):
            return False
    for index_name, (expected_columns, expected_unique) in EXPECTED_INDEXES[table_name].items():
        index = indexes.get(index_name)
        if index is None:
            return False
        if index['origin'] != 'c':
            return False
        if index['columns'] != expected_columns:
            return False
        if index['unique'] is not expected_unique:
            return False
    return True


def _normalized_check(sqltext):
    return ' '.join((sqltext or '').strip().split()).replace('"', "'")


def _check_references_column(sqltext, column_name):
    expression = sqltext or ''
    position = 0
    while position < len(expression):
        char = expression[position]
        if char == "'":
            position += 1
            while position < len(expression):
                if expression[position] == "'":
                    if position + 1 < len(expression) and expression[position + 1] == "'":
                        position += 2
                        continue
                    position += 1
                    break
                position += 1
            continue
        if char == '"' or char == '`':
            quote = char
            position += 1
            start = position
            while position < len(expression) and expression[position] != quote:
                position += 1
            token = expression[start:position]
            if position < len(expression):
                position += 1
            if token.lower() == column_name.lower():
                return True
            continue
        if char == '_' or char.isalpha():
            start = position
            position += 1
            while position < len(expression) and (expression[position] == '_' or expression[position].isalnum()):
                position += 1
            if expression[start:position].lower() == column_name.lower():
                return True
            continue
        position += 1
    return False


def _has_expected_checks(engine, table_name):
    checks = [_normalized_check(check.get('sqltext') or '') for check in inspect(engine).get_check_constraints(table_name)]
    expected_checks = {_normalized_check(check) for check in EXPECTED_CHECKS[table_name]}
    return len(checks) == len(expected_checks) and set(checks) == expected_checks


def _has_exact_foreign_keys(engine, table_name):
    actual_fks = _actual_fks(engine, table_name)
    if actual_fks is None:
        return False
    return len(actual_fks) == len(EXPECTED_FKS[table_name]) and set(actual_fks) == EXPECTED_FKS[table_name]


def _has_exact_uniques(engine, table_name):
    actual_uniques = _actual_uniques(engine, table_name)
    if actual_uniques is None:
        return False
    return len(actual_uniques) == len(EXPECTED_UNIQUES[table_name]) and set(actual_uniques) == EXPECTED_UNIQUES[table_name]


def table_is_compatible(engine, table_name):
    columns = _column_map(engine, table_name)
    if set(columns) != EXPECTED_COLUMNS[table_name]:
        return False
    if not _has_exact_primary_key(columns):
        return False
    for column_name in EXPECTED_NONNULL[table_name]:
        if columns[column_name].get('nullable'):
            return False
    for column_name, column in columns.items():
        if column_name not in EXPECTED_NONNULL[table_name] and not column.get('nullable'):
            return False
    for column_name, expected_length in EXPECTED_LENGTHS[table_name].items():
        if not _is_varchar_column(columns[column_name]):
            return False
        if _column_length(columns[column_name]) != expected_length:
            return False
    for column_name in ('balance', 'amount'):
        if column_name in columns and not _is_integer_column(columns[column_name]):
            return False
    for column_name in EXPECTED_DATETIME_COLUMNS[table_name]:
        if not _is_datetime_column(columns[column_name]):
            return False
    if not _has_exact_foreign_keys(engine, table_name):
        return False
    if not _has_exact_uniques(engine, table_name):
        return False
    return _has_indexes(engine, table_name) and _has_expected_checks(engine, table_name)


def payment_tables_are_compatible(engine):
    if engine.dialect.name != 'sqlite':
        return False
    names = _table_names(engine)
    for table_name in PAYMENT_TABLES:
        if table_name in names and not table_is_compatible(engine, table_name):
            return False
    return True


def _foreign_key_check(engine):
    with engine.connect() as connection:
        return connection.execute(text('PRAGMA foreign_key_check')).all()


def migrate_point_payments_schema(engine):
    if engine.dialect.name != 'sqlite':
        print('Unsupported database dialect. No changes were made.')
        return 2

    if not payment_tables_are_compatible(engine):
        print('Existing point-payment table needs manual recovery. No changes were made.')
        return 2

    if _foreign_key_check(engine):
        print('Foreign-key validation failed. No changes were made.')
        return 1

    try:
        from market.models import PointWallet, TradePayment, PointLedger

        existing = _table_names(engine)
        if 'point_wallet' not in existing:
            PointWallet.__table__.create(bind=engine, checkfirst=True)
        existing = _table_names(engine)
        if 'trade_payment' not in existing:
            TradePayment.__table__.create(bind=engine, checkfirst=True)
        existing = _table_names(engine)
        if 'point_ledger' not in existing:
            PointLedger.__table__.create(bind=engine, checkfirst=True)
    except SQLAlchemyError:
        print('Point-payment migration failed. No internal details were printed.')
        return 1

    if not payment_tables_are_compatible(engine):
        print('Point-payment migration compatibility check failed.')
        return 1
    if _foreign_key_check(engine):
        print('Foreign-key validation failed after point-payment migration.')
        return 1

    print('Point-payment tables are ready.')
    return 0


def main():
    try:
        from market import create_app
        from market.extensions import db

        app = create_app()
        with app.app_context():
            return migrate_point_payments_schema(db.engine)
    except SQLAlchemyError:
        print('Point-payment migration failed. No internal details were printed.')
        return 1
    except RuntimeError:
        print('Application configuration is incomplete. No changes were made.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
