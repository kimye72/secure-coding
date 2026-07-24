import re
import unicodedata


MAX_KEYWORD_LENGTH = 80
MAX_NORMALIZED_KEYWORD_LENGTH = 240
MAX_KEYWORD_SUBSCRIPTIONS_PER_USER = 20


class KeywordValidationError(ValueError):
    """Raised when a keyword cannot be safely stored or matched."""


def normalize_keyword(raw_value):
    """Return (display_keyword, normalized_keyword) for a user keyword."""
    value = _normalize_separators(unicodedata.normalize('NFKC', raw_value or ''))
    if not value:
        raise KeywordValidationError()

    display_keyword = re.sub(r' +', ' ', value)
    if not display_keyword or len(display_keyword) > MAX_KEYWORD_LENGTH:
        raise KeywordValidationError()

    normalized_keyword = normalize_for_match(display_keyword)
    if len(normalized_keyword) > MAX_NORMALIZED_KEYWORD_LENGTH:
        raise KeywordValidationError()

    return display_keyword, normalized_keyword


def normalize_for_match(value):
    normalized = _normalize_separators(
        unicodedata.normalize('NFKC', value or ''),
        reject_invalid=False,
    ).casefold()
    return re.sub(r' +', ' ', normalized.strip())


def _normalize_separators(value, reject_invalid=True):
    characters = []
    for char in value:
        category = unicodedata.category(char)
        if category.startswith('C') or category in {'Zl', 'Zp'}:
            if reject_invalid:
                raise KeywordValidationError()
            characters.append(' ')
            continue
        if category == 'Zs':
            characters.append(' ')
        else:
            characters.append(char)
    return ''.join(characters).strip()


def matching_subscriptions_for_product(subscriptions, title, description):
    """Return one deterministic matching subscription per recipient user."""
    haystack = f'{normalize_for_match(title)} {normalize_for_match(description)}'
    matches_by_user = {}

    for subscription in subscriptions:
        if subscription.normalized_keyword in haystack and subscription.user_id not in matches_by_user:
            matches_by_user[subscription.user_id] = subscription

    return list(matches_by_user.values())
