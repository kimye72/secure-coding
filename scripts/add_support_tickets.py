import sys
import re

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError


SUPPORT_TICKET_TABLE = 'support_ticket'
EXPECTED_SUPPORT_INDEXES = {
    'ix_support_ticket_created_at': ('created_at',),
    'ix_support_ticket_status': ('status',),
    'ix_support_ticket_user_id': ('user_id',),
}
EXPECTED_CATEGORY_VALUES = {'BUG', 'TRADE', 'ACCOUNT', 'OTHER'}
EXPECTED_STATUS_VALUES = {'OPEN', 'IN_PROGRESS', 'RESOLVED'}


def _table_names(engine):
    return set(inspect(engine).get_table_names())


def _foreign_key_check(engine):
    with engine.connect() as connection:
        return connection.execute(text('PRAGMA foreign_key_check')).all()


def _column_map(engine, table_name):
    return {column['name']: column for column in inspect(engine).get_columns(table_name)}


def _column_length(column):
    return getattr(column['type'], 'length', None)


def _has_lookup_indexes(engine):
    indexes = {
        index['name']: tuple(index.get('column_names') or ())
        for index in inspect(engine).get_indexes(SUPPORT_TICKET_TABLE)
    }
    for index_name, expected_columns in EXPECTED_SUPPORT_INDEXES.items():
        if indexes.get(index_name) != expected_columns:
            return False
    return True


def _has_user_foreign_key(engine):
    foreign_keys = inspect(engine).get_foreign_keys(SUPPORT_TICKET_TABLE)
    actual_fks = {
        (fk['constrained_columns'][0], fk['referred_table'], fk['referred_columns'][0])
        for fk in foreign_keys
        if len(fk.get('constrained_columns', [])) == 1 and len(fk.get('referred_columns', [])) == 1
    }
    return ('user_id', 'user', 'id') in actual_fks


def _constraint_values_for_column(sqltext, column_name):
    expression = _strip_balanced_outer_parentheses(' '.join((sqltext or '').strip().split()))
    if not expression:
        return None

    pattern = re.compile(
        rf'"?{re.escape(column_name)}"?\s+IN\s*\((?P<values>[^()]*)\)',
        re.IGNORECASE,
    )
    match = pattern.fullmatch(expression)
    if match is None:
        return None

    values_text = match.group('values')
    return _parse_quoted_literal_list(values_text)


def _strip_balanced_outer_parentheses(expression):
    expression = expression.strip()
    while expression.startswith('(') and expression.endswith(')'):
        depth = 0
        wraps_entire_expression = True
        in_quote = None
        index = 0
        while index < len(expression):
            char = expression[index]
            if in_quote:
                if char == in_quote:
                    if index + 1 < len(expression) and expression[index + 1] == in_quote:
                        index += 2
                        continue
                    in_quote = None
                index += 1
                continue
            if char in {"'", '"'}:
                in_quote = char
            elif char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth < 0:
                    return expression
                if depth == 0 and index != len(expression) - 1:
                    wraps_entire_expression = False
                    break
            index += 1

        if depth != 0 or in_quote or not wraps_entire_expression:
            return expression
        expression = expression[1:-1].strip()
    return expression


def _parse_quoted_literal_list(values_text):
    values = []
    position = 0
    length = len(values_text)

    while position < length:
        while position < length and values_text[position].isspace():
            position += 1
        if position >= length:
            return None

        quote = values_text[position]
        if quote not in {"'", '"'}:
            return None
        position += 1

        characters = []
        while position < length:
            char = values_text[position]
            if char == quote:
                if position + 1 < length and values_text[position + 1] == quote:
                    characters.append(quote)
                    position += 2
                    continue
                position += 1
                break
            characters.append(char)
            position += 1
        else:
            return None

        values.append(''.join(characters))

        while position < length and values_text[position].isspace():
            position += 1
        if position == length:
            break
        if values_text[position] != ',':
            return None
        position += 1

    if not values:
        return None
    return set(values)


def _check_expression_references_column(sqltext, column_name):
    expression = sqltext or ''
    position = 0
    length = len(expression)
    lowered_column = column_name.lower()

    while position < length:
        char = expression[position]

        if char == "'":
            position += 1
            while position < length:
                if expression[position] == "'":
                    if position + 1 < length and expression[position + 1] == "'":
                        position += 2
                        continue
                    position += 1
                    break
                position += 1
            continue

        if char == '"':
            position += 1
            quoted = []
            while position < length:
                if expression[position] == '"':
                    if position + 1 < length and expression[position + 1] == '"':
                        quoted.append('"')
                        position += 2
                        continue
                    position += 1
                    break
                quoted.append(expression[position])
                position += 1
            if ''.join(quoted).lower() == lowered_column:
                return True
            continue

        if char == '[':
            position += 1
            quoted = []
            while position < length and expression[position] != ']':
                quoted.append(expression[position])
                position += 1
            if position < length:
                position += 1
            if ''.join(quoted).lower() == lowered_column:
                return True
            continue

        if char == '`':
            position += 1
            quoted = []
            while position < length:
                if expression[position] == '`':
                    if position + 1 < length and expression[position + 1] == '`':
                        quoted.append('`')
                        position += 2
                        continue
                    position += 1
                    break
                quoted.append(expression[position])
                position += 1
            if ''.join(quoted).lower() == lowered_column:
                return True
            continue

        if char == '_' or char.isalpha():
            start = position
            position += 1
            while position < length and (expression[position] == '_' or expression[position].isalnum()):
                position += 1
            if expression[start:position].lower() == lowered_column:
                return True
            continue

        position += 1

    return False


def _has_exact_check_constraint(engine, column_name, expected_values):
    exact_constraint_count = 0
    for constraint in inspect(engine).get_check_constraints(SUPPORT_TICKET_TABLE):
        sqltext = constraint.get('sqltext') or ''
        values = _constraint_values_for_column(sqltext, column_name)
        if values == expected_values:
            exact_constraint_count += 1
        elif _check_expression_references_column(sqltext, column_name):
            return False
    return exact_constraint_count == 1



def _has_expected_check_constraints(engine):
    return (
        _has_exact_check_constraint(engine, 'category', EXPECTED_CATEGORY_VALUES)
        and _has_exact_check_constraint(engine, 'status', EXPECTED_STATUS_VALUES)
    )


def _has_exact_primary_key(columns):
    if columns['id'].get('primary_key') != 1:
        return False
    if columns['id'].get('nullable'):
        return False
    for column_name, column in columns.items():
        if column_name != 'id' and column.get('primary_key'):
            return False
    return True


def support_ticket_table_is_compatible(engine):
    if engine.dialect.name != 'sqlite':
        return False
    if SUPPORT_TICKET_TABLE not in _table_names(engine):
        return True

    expected_columns = {
        'id',
        'user_id',
        'category',
        'title',
        'content',
        'status',
        'admin_response',
        'created_at',
        'updated_at',
    }
    columns = _column_map(engine, SUPPORT_TICKET_TABLE)
    if set(columns) != expected_columns:
        return False

    if not _has_exact_primary_key(columns):
        return False
    for column_name in ('user_id', 'category', 'title', 'content', 'status', 'created_at', 'updated_at'):
        if columns[column_name].get('nullable'):
            return False
    if not columns['admin_response'].get('nullable'):
        return False

    expected_lengths = {
        'id': 36,
        'user_id': 36,
        'category': 20,
        'title': 120,
        'content': 2000,
        'status': 20,
        'admin_response': 2000,
    }
    for column_name, expected_length in expected_lengths.items():
        if _column_length(columns[column_name]) != expected_length:
            return False

    return (
        _has_user_foreign_key(engine)
        and _has_expected_check_constraints(engine)
        and _has_lookup_indexes(engine)
    )


def migrate_support_tickets_schema(engine):
    if engine.dialect.name != 'sqlite':
        print('Unsupported database dialect. No changes were made.')
        return 2

    if not support_ticket_table_is_compatible(engine):
        print('Existing support ticket table needs manual recovery. No changes were made.')
        return 2

    if _foreign_key_check(engine):
        print('Foreign-key validation failed. No changes were made.')
        return 1

    if SUPPORT_TICKET_TABLE in _table_names(engine):
        print('support_ticket table already exists. No changes were needed.')
        return 0

    try:
        from market.models import SupportTicket

        SupportTicket.__table__.create(bind=engine, checkfirst=True)
    except SQLAlchemyError:
        print('Support ticket migration failed. No internal details were printed.')
        return 1

    if _foreign_key_check(engine):
        print('Foreign-key validation failed after support ticket migration.')
        return 1

    print('support_ticket table was created successfully.')
    return 0


def main():
    try:
        from market import create_app
        from market.extensions import db

        app = create_app()
        with app.app_context():
            return migrate_support_tickets_schema(db.engine)
    except SQLAlchemyError:
        print('Support ticket migration failed. No internal details were printed.')
        return 1
    except RuntimeError:
        print('Application configuration is incomplete. No changes were made.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
