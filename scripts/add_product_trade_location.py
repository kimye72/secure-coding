import sys

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError


def add_trade_location_column(engine):
    """Add product.trade_location to a SQLite database if it is missing.

    This function is intentionally narrow: fixed table name, fixed column name,
    no command-line table/column input, no data copying, and no destructive DDL.
    """
    if engine.dialect.name != 'sqlite':
        print('Unsupported database dialect. No changes were made.')
        return 2

    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    if 'product' not in table_names:
        print('Product table was not found. No changes were made.')
        return 2

    column_names = {column['name'] for column in inspector.get_columns('product')}
    if 'trade_location' in column_names:
        print('product.trade_location already exists. No changes were needed.')
        return 0

    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE product ADD COLUMN trade_location VARCHAR(120)'))

    print('product.trade_location was added successfully.')
    return 0


def main():
    try:
        from market import create_app
        from market.extensions import db

        app = create_app()
        with app.app_context():
            return add_trade_location_column(db.engine)
    except SQLAlchemyError:
        print('Database migration failed. No internal details were printed.')
        return 1
    except RuntimeError:
        print('Application configuration is incomplete. No changes were made.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
