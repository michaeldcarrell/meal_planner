"""Scale free-text ingredient quantities ("1 1/2 cups", "1-2 tsp", "2 cans (15 oz each)") for a different
number of servings.

Only the leading amount is scaled, plus an amount in parentheses after it when that's a total:
"(about 1.5 lb)", or "4 fillets (1.5 lb)". A single item's size ("1 can (15 oz)") or a per-item size
("2 cans (15 oz each)") stays as written. Anything without a leading amount ("to taste") is unchanged.
"""
import re
from fractions import Fraction

UNICODE_FRACTIONS = {'½': '1/2', '⅓': '1/3', '⅔': '2/3', '¼': '1/4', '¾': '3/4', '⅛': '1/8', '⅜': '3/8', '⅝': '5/8',
                     '⅞': '7/8'}
_NUMBER = r'(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)'
_AMOUNT = re.compile(rf'^\s*(?P<low>{_NUMBER})(?:\s*(?:-|–|to)\s*(?P<high>{_NUMBER}))?(?P<rest>.*)$', re.DOTALL)
_PAREN = re.compile(rf'\((?P<about>(?:about|approx\.?|roughly)\s+)?(?P<amount>{_NUMBER})(?P<after>[^)]*)\)', re.IGNORECASE)
# Whole items: the word after the amount follows it into the plural ("1 can" → "2 cans") and back.
_COUNT_WORDS = {
    'can': 'cans', 'bag': 'bags', 'box': 'boxes', 'bunch': 'bunches', 'clove': 'cloves', 'head': 'heads',
    'stalk': 'stalks', 'package': 'packages', 'pouch': 'pouches', 'loaf': 'loaves', 'pint': 'pints',
    'fillet': 'fillets', 'piece': 'pieces', 'crown': 'crowns', 'jar': 'jars', 'bottle': 'bottles',
    'sprig': 'sprigs', 'slice': 'slices', 'link': 'links', 'block': 'blocks', 'container': 'containers',
    'cup': 'cups', 'breast': 'breasts', 'thigh': 'thighs', 'tortilla': 'tortillas', 'egg': 'eggs',
}
_SINGULAR = {plural: single for single, plural in _COUNT_WORDS.items()}
# Kitchen-friendly fractions; amounts are rounded to the nearest of these.
_DENOMINATORS = (2, 3, 4, 8)


def _parse(number: str) -> Fraction:
    parts = number.split()
    return sum((Fraction(p) for p in parts), Fraction(0))


def _format(value: Fraction, decimal: bool) -> str:
    if decimal:
        return f'{float(value):.2f}'.rstrip('0').rstrip('.')
    whole = int(value)
    remainder = value - whole
    if remainder == 0:
        return str(whole)
    nearest = min((Fraction(round(remainder * d), d) for d in _DENOMINATORS), key=lambda f: abs(f - remainder))
    if nearest == 0:
        return str(whole) if whole else f'1/{_DENOMINATORS[-1]}'
    if nearest == 1:
        return str(whole + 1)
    return f'{whole} {nearest}' if whole else str(nearest)


def _scale_number(number: str, factor: Fraction) -> tuple[str, Fraction]:
    value = _parse(number) * factor
    return _format(value, decimal='.' in number), value


def _fix_count_word(rest: str, amount: Fraction) -> str:
    """'2 can' → '2 cans', '1 cans' → '1 can' for whole-item words right after the amount."""
    match = re.match(r'^(\s+)([A-Za-z]+)\b', rest)
    if not match:
        return rest
    word = match.group(2)
    lower = word.lower()
    if amount > 1 and lower in _COUNT_WORDS:
        replacement = _COUNT_WORDS[lower]
    elif amount <= 1 and lower in _SINGULAR:
        replacement = _SINGULAR[lower]
    else:
        return rest
    if word[0].isupper():
        replacement = replacement.capitalize()
    return match.group(1) + replacement + rest[match.end():]


def scale_quantity(text: str, factor) -> str:
    """`text` with its amounts multiplied by `factor` (e.g. Fraction(6, 4) for 6 servings of a 4-serving recipe)."""
    factor = Fraction(factor)
    if factor == 1 or not text:
        return text
    normalized = text
    for symbol, fraction in UNICODE_FRACTIONS.items():
        normalized = re.sub(rf'(\d)\s*{symbol}', rf'\1 {fraction}', normalized).replace(symbol, fraction)
    match = _AMOUNT.match(normalized)
    if not match:
        return text
    low, low_value = _scale_number(match.group('low'), factor)
    amount, top = low, low_value
    if match.group('high'):
        high, top = _scale_number(match.group('high'), factor)
        amount = f'{low}-{high}'
    several = _parse(match.group('low')) != 1

    def paren(m):
        if not (m.group('about') or (several and 'each' not in m.group('after').lower())):
            return m.group(0)
        number, value = _scale_number(m.group('amount'), factor)
        return f"({m.group('about') or ''}{number}{_fix_count_word(m.group('after'), value)})"

    return amount + _fix_count_word(_PAREN.sub(paren, match.group('rest')), top)
