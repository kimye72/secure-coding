import re
import unicodedata


MAX_SUPPORT_TITLE_LENGTH = 120
MAX_SUPPORT_CONTENT_LENGTH = 2000


class SupportTicketValidationError(ValueError):
    """Raised when support-ticket input cannot be safely stored."""


def normalize_support_title(raw_value):
    value = _normalize_text(raw_value or '', allow_newlines=False)
    value = re.sub(r' +', ' ', value).strip()
    if not value or len(value) > MAX_SUPPORT_TITLE_LENGTH:
        raise SupportTicketValidationError()
    return value


def normalize_support_body(raw_value, *, required):
    value = unicodedata.normalize('NFKC', raw_value or '')
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    value = _normalize_text(value, allow_newlines=True).strip()

    if not value:
        if required:
            raise SupportTicketValidationError()
        return None
    if len(value) > MAX_SUPPORT_CONTENT_LENGTH:
        raise SupportTicketValidationError()
    return value


def _normalize_text(value, *, allow_newlines):
    normalized = unicodedata.normalize('NFKC', value)
    characters = []
    for char in normalized:
        if allow_newlines and char == '\n':
            characters.append('\n')
            continue

        category = unicodedata.category(char)
        if category.startswith('C') or category in {'Zl', 'Zp'}:
            raise SupportTicketValidationError()
        if category == 'Zs':
            characters.append(' ')
        else:
            characters.append(char)
    return ''.join(characters)
