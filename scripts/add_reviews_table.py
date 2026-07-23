import sys

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError


EXPECTED_TRADE_COLUMNS = (
    'id',
    'product_id',
    'buyer_id',
    'seller_id',
    'status',
    'created_at',
    'updated_at',
)
EXPECTED_TRADE_INDEXES = {
    'ix_trade_buyer_id',
    'ix_trade_product_id',
    'ix_trade_seller_id',
}


def _sqlite_master_sql(connection, object_type, name):
    row = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type = :type AND name = :name"),
        {'type': object_type, 'name': name},
    ).first()
    if row is None:
        return None
    return row[0]


def _sqlite_table_names(engine):
    return set(inspect(engine).get_table_names())


def trade_status_supports_completed(engine):
    if engine.dialect.name != 'sqlite':
        return False
    if 'trade' not in _sqlite_table_names(engine):
        return False
    with engine.connect() as connection:
        sql = _sqlite_master_sql(connection, 'table', 'trade')
    return bool(sql and 'COMPLETED' in sql)


def _trade_schema_matches_expected(engine):
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if 'trade' not in table_names:
        return False

    columns = inspector.get_columns('trade')
    if [column['name'] for column in columns] != list(EXPECTED_TRADE_COLUMNS):
        return False

    column_map = {column['name']: column for column in columns}
    if not column_map['id'].get('primary_key'):
        return False

    for name in EXPECTED_TRADE_COLUMNS:
        if name != 'id' and column_map[name].get('nullable'):
            return False

    foreign_keys = inspector.get_foreign_keys('trade')
    expected_fks = {
        ('product_id', 'product', 'id'),
        ('buyer_id', 'user', 'id'),
        ('seller_id', 'user', 'id'),
    }
    actual_fks = {
        (fk['constrained_columns'][0], fk['referred_table'], fk['referred_columns'][0])
        for fk in foreign_keys
        if len(fk.get('constrained_columns', [])) == 1 and len(fk.get('referred_columns', [])) == 1
    }
    if actual_fks != expected_fks:
        return False

    index_names = {index['name'] for index in inspector.get_indexes('trade')}
    if not EXPECTED_TRADE_INDEXES.issubset(index_names):
        return False

    with engine.connect() as connection:
        sql = _sqlite_master_sql(connection, 'table', 'trade')
    if not sql:
        return False
    if 'buyer_id != seller_id' not in sql:
        return False
    if 'PENDING' not in sql or 'ACCEPTED' not in sql or 'REJECTED' not in sql or 'CANCELLED' not in sql:
        return False

    return True


def _review_table_is_compatible(engine):
    inspector = inspect(engine)
    if 'review' not in set(inspector.get_table_names()):
        return True

    expected_columns = {
        'id',
        'trade_id',
        'product_id',
        'reviewer_id',
        'reviewee_id',
        'rating',
        'content',
        'created_at',
    }
    actual_columns = {column['name'] for column in inspector.get_columns('review')}
    if actual_columns != expected_columns:
        return False

    with engine.connect() as connection:
        sql = _sqlite_master_sql(connection, 'table', 'review')
    if not sql:
        return False
    if 'rating >= 1 AND rating <= 5' not in sql:
        return False
    if 'reviewer_id != reviewee_id' not in sql:
        return False
    if 'UNIQUE (trade_id, reviewer_id)' not in sql:
        return False

    return True


def _trade_index_sql(connection):
    rows = connection.execute(text(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = 'trade' AND sql IS NOT NULL "
        "ORDER BY name"
    )).all()
    return [(row[0], row[1]) for row in rows]


def _foreign_key_setting(connection):
    return int(connection.execute(text('PRAGMA foreign_keys')).scalar() or 0)


def _set_foreign_keys(connection, enabled):
    connection.execute(text(f'PRAGMA foreign_keys={"ON" if enabled else "OFF"}'))


def _foreign_key_check(connection):
    return connection.execute(text('PRAGMA foreign_key_check')).all()


def _restore_foreign_key_setting(engine, connection, original_fk_setting):
    try:
        connection.commit()
        _set_foreign_keys(connection, bool(original_fk_setting))
        connection.commit()
        if _foreign_key_setting(connection) != original_fk_setting:
            raise RuntimeError('foreign-key-restore-mismatch')
        return True
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        engine.dispose()
        return False


def migrate_trade_status_constraint(engine):
    if engine.dialect.name != 'sqlite':
        print('Unsupported database dialect. No changes were made.')
        return 2

    if 'trade_new' in _sqlite_table_names(engine):
        print('Temporary trade migration table already exists. Manual recovery is required.')
        return 2

    if trade_status_supports_completed(engine):
        print('trade.ck_trade_status already permits COMPLETED. No trade-table changes were needed.')
        return 0

    if not _trade_schema_matches_expected(engine):
        print('Trade table schema is not in the expected form. Manual migration is required.')
        return 2

    with engine.connect() as connection:
        original_fk_setting = _foreign_key_setting(connection)
        index_sql = _trade_index_sql(connection)
        before_count = connection.execute(text('SELECT COUNT(*) FROM trade')).scalar()
        result = 1

        try:
            connection.commit()
            _set_foreign_keys(connection, False)
            connection.commit()

            connection.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                connection.execute(text(
                    'CREATE TABLE trade_new ('
                    'id VARCHAR(36) NOT NULL, '
                    'product_id VARCHAR(36) NOT NULL, '
                    'buyer_id VARCHAR(36) NOT NULL, '
                    'seller_id VARCHAR(36) NOT NULL, '
                    'status VARCHAR(20) NOT NULL, '
                    'created_at DATETIME NOT NULL, '
                    'updated_at DATETIME NOT NULL, '
                    'PRIMARY KEY (id), '
                    'CONSTRAINT ck_trade_buyer_ne_seller CHECK (buyer_id != seller_id), '
                    "CONSTRAINT ck_trade_status CHECK (status IN ('PENDING', 'ACCEPTED', 'REJECTED', 'CANCELLED', 'COMPLETED')), "
                    'FOREIGN KEY(product_id) REFERENCES product (id), '
                    'FOREIGN KEY(buyer_id) REFERENCES "user" (id), '
                    'FOREIGN KEY(seller_id) REFERENCES "user" (id)'
                    ')'
                ))
                connection.execute(text(
                    'INSERT INTO trade_new (id, product_id, buyer_id, seller_id, status, created_at, updated_at) '
                    'SELECT id, product_id, buyer_id, seller_id, status, created_at, updated_at FROM trade'
                ))
                after_copy_count = connection.execute(text('SELECT COUNT(*) FROM trade_new')).scalar()
                if before_count != after_copy_count:
                    raise RuntimeError('row-count-mismatch')

                connection.execute(text('DROP TABLE trade'))
                connection.execute(text('ALTER TABLE trade_new RENAME TO trade'))
                for _name, sql in index_sql:
                    connection.execute(text(sql))

                after_count = connection.execute(text('SELECT COUNT(*) FROM trade')).scalar()
                if before_count != after_count:
                    raise RuntimeError('row-count-mismatch')

                if _foreign_key_check(connection):
                    raise RuntimeError('foreign-key-check-failed')

                connection.exec_driver_sql('COMMIT')
            except Exception:
                connection.exec_driver_sql('ROLLBACK')
                raise

            print('trade.ck_trade_status was updated to permit COMPLETED.')
            result = 0
        except Exception:
            print('Trade status migration failed. No internal details were printed.')
            result = 1
        finally:
            if not _restore_foreign_key_setting(engine, connection, original_fk_setting):
                print('Foreign-key setting restoration failed. Manual recovery is required.')
                result = 1

        return result


def create_reviews_table(engine):
    """Create the review table if missing after validating partial state."""
    if engine.dialect.name != 'sqlite':
        print('Unsupported database dialect. No changes were made.')
        return 2

    if not _review_table_is_compatible(engine):
        print('Existing review table needs manual recovery. No review-table changes were made.')
        return 2

    inspector = inspect(engine)
    if 'review' in inspector.get_table_names():
        print('review table already exists. No changes were needed.')
        return 0

    from market.models import Review

    Review.__table__.create(bind=engine, checkfirst=True)
    with engine.connect() as connection:
        if _foreign_key_check(connection):
            print('Foreign-key validation failed after review migration.')
            return 1
    print('review table was created successfully.')
    return 0


def migrate_reviews_schema(engine):
    if engine.dialect.name != 'sqlite':
        print('Unsupported database dialect. No changes were made.')
        return 2

    if not _review_table_is_compatible(engine):
        print('Existing review table needs manual recovery. No changes were made.')
        return 2

    trade_result = migrate_trade_status_constraint(engine)
    if trade_result != 0:
        return trade_result

    return create_reviews_table(engine)


def main():
    try:
        from market import create_app
        from market.extensions import db

        app = create_app()
        with app.app_context():
            return migrate_reviews_schema(db.engine)
    except SQLAlchemyError:
        print('Review migration failed. No internal details were printed.')
        return 1
    except RuntimeError:
        print('Application configuration is incomplete. No changes were made.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
