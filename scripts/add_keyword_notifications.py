import sys

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from market.keyword_notifications import MAX_KEYWORD_LENGTH, MAX_NORMALIZED_KEYWORD_LENGTH


KEYWORD_SUBSCRIPTION_TABLE = 'keyword_subscription'
NOTIFICATION_TABLE = 'notification'
EXPECTED_KEYWORD_SUBSCRIPTION_INDEXES = {'ix_keyword_subscription_user_id'}
EXPECTED_NOTIFICATION_INDEXES = {'ix_notification_product_id', 'ix_notification_user_id'}


def _table_names(engine):
    return set(inspect(engine).get_table_names())


def _table_sql(connection, table_name):
    row = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {'name': table_name},
    ).first()
    if row is None:
        return None
    return row[0]


def _foreign_key_check(engine):
    with engine.connect() as connection:
        return connection.execute(text('PRAGMA foreign_key_check')).all()


def _has_unique_constraint(engine, table_name, columns):
    expected = tuple(columns)
    for constraint in inspect(engine).get_unique_constraints(table_name):
        if tuple(constraint.get('column_names') or ()) == expected:
            return True
    return False


def _column_map(engine, table_name):
    return {column['name']: column for column in inspect(engine).get_columns(table_name)}


def _column_length(column):
    return getattr(column['type'], 'length', None)


def _column_is_boolean(column):
    type_name = column['type'].__class__.__name__.lower()
    return type_name == 'boolean'


def _has_lookup_indexes(engine, table_name, expected_index_names):
    index_names = {index['name'] for index in inspect(engine).get_indexes(table_name)}
    return expected_index_names.issubset(index_names)


def _keyword_subscription_table_is_compatible(engine):
    if KEYWORD_SUBSCRIPTION_TABLE not in _table_names(engine):
        return True

    inspector = inspect(engine)
    expected_columns = {
        'id',
        'user_id',
        'keyword',
        'normalized_keyword',
        'created_at',
    }
    columns = _column_map(engine, KEYWORD_SUBSCRIPTION_TABLE)
    actual_columns = set(columns)
    if actual_columns != expected_columns:
        return False
    if not columns['id'].get('primary_key'):
        return False
    for column_name in ('user_id', 'keyword', 'normalized_keyword', 'created_at'):
        if columns[column_name].get('nullable'):
            return False
    if _column_length(columns['keyword']) != MAX_KEYWORD_LENGTH:
        return False
    if _column_length(columns['normalized_keyword']) != MAX_NORMALIZED_KEYWORD_LENGTH:
        return False
    if not _has_unique_constraint(engine, KEYWORD_SUBSCRIPTION_TABLE, ('user_id', 'normalized_keyword')):
        return False
    if not _has_lookup_indexes(engine, KEYWORD_SUBSCRIPTION_TABLE, EXPECTED_KEYWORD_SUBSCRIPTION_INDEXES):
        return False

    foreign_keys = inspector.get_foreign_keys(KEYWORD_SUBSCRIPTION_TABLE)
    expected_fks = {('user_id', 'user', 'id')}
    actual_fks = {
        (fk['constrained_columns'][0], fk['referred_table'], fk['referred_columns'][0])
        for fk in foreign_keys
        if len(fk.get('constrained_columns', [])) == 1 and len(fk.get('referred_columns', [])) == 1
    }
    return actual_fks == expected_fks


def _notification_table_is_compatible(engine):
    if NOTIFICATION_TABLE not in _table_names(engine):
        return True

    inspector = inspect(engine)
    expected_columns = {
        'id',
        'user_id',
        'product_id',
        'matched_keyword',
        'is_read',
        'created_at',
    }
    columns = _column_map(engine, NOTIFICATION_TABLE)
    actual_columns = set(columns)
    if actual_columns != expected_columns:
        return False
    if not columns['id'].get('primary_key'):
        return False
    for column_name in ('user_id', 'product_id', 'matched_keyword', 'is_read', 'created_at'):
        if columns[column_name].get('nullable'):
            return False
    if _column_length(columns['matched_keyword']) != MAX_KEYWORD_LENGTH:
        return False
    if not _column_is_boolean(columns['is_read']):
        return False
    if not _has_unique_constraint(engine, NOTIFICATION_TABLE, ('user_id', 'product_id')):
        return False
    if not _has_lookup_indexes(engine, NOTIFICATION_TABLE, EXPECTED_NOTIFICATION_INDEXES):
        return False

    foreign_keys = inspector.get_foreign_keys(NOTIFICATION_TABLE)
    expected_fks = {
        ('user_id', 'user', 'id'),
        ('product_id', 'product', 'id'),
    }
    actual_fks = {
        (fk['constrained_columns'][0], fk['referred_table'], fk['referred_columns'][0])
        for fk in foreign_keys
        if len(fk.get('constrained_columns', [])) == 1 and len(fk.get('referred_columns', [])) == 1
    }
    return actual_fks == expected_fks


def notification_tables_are_compatible(engine):
    if engine.dialect.name != 'sqlite':
        return False
    return (
        _keyword_subscription_table_is_compatible(engine)
        and _notification_table_is_compatible(engine)
    )


def migrate_keyword_notifications_schema(engine):
    if engine.dialect.name != 'sqlite':
        print('Unsupported database dialect. No changes were made.')
        return 2

    if not notification_tables_are_compatible(engine):
        print('Existing notification tables need manual recovery. No changes were made.')
        return 2

    if _foreign_key_check(engine):
        print('Foreign-key validation failed. No changes were made.')
        return 1

    from market.models import KeywordSubscription, Notification

    try:
        existing_tables = _table_names(engine)
        with engine.begin() as connection:
            if KEYWORD_SUBSCRIPTION_TABLE not in existing_tables:
                KeywordSubscription.__table__.create(bind=connection, checkfirst=True)
            if NOTIFICATION_TABLE not in existing_tables:
                Notification.__table__.create(bind=connection, checkfirst=True)
    except SQLAlchemyError:
        print('Notification migration failed. No internal details were printed.')
        return 1

    if _foreign_key_check(engine):
        print('Foreign-key validation failed after notification migration.')
        return 1

    print('Keyword subscription and notification tables are ready.')
    return 0


def main():
    try:
        from market import create_app
        from market.extensions import db

        app = create_app()
        with app.app_context():
            return migrate_keyword_notifications_schema(db.engine)
    except SQLAlchemyError:
        print('Notification migration failed. No internal details were printed.')
        return 1
    except RuntimeError:
        print('Application configuration is incomplete. No changes were made.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
