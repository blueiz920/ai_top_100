"""Keep an explicitly labelled contest case ID separate from authentication.

The contest says the ID is delivered in the dialogue, but its exact evaluation
format has not been verified. Only labelled forms are recognized here; a case
ID never authenticates a customer or proves ownership of an order.
"""

import re


_LABEL = r'(?:TC(?:[ \t]*ID|[ \t]*번호)?|테스트[ \t]*케이스(?:[ \t]*ID|[ \t]*번호)?)'
_IDENTIFIER = r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}'
_CASE = re.compile(
    rf'(?im)^[ \t]*(?:\[[ \t]*{_LABEL}[ \t]*[:：=][ \t]*(?P<bracket>{_IDENTIFIER})[ \t]*\]'
    rf'|{_LABEL}[ \t]*[:：=][ \t]*(?P<line>{_IDENTIFIER})[ \t]*$)'
)
_MARKER = re.compile(rf'(?im)^[ \t]*\[?[ \t]*{_LABEL}[ \t]*[:：=]')


def has_case_marker(content):
    return isinstance(content, str) and bool(_MARKER.search(content))


def starts_with_case_marker(content):
    return isinstance(content, str) and bool(_MARKER.match(content))


def parse_case_id(content):
    if not isinstance(content, str):
        return None
    values = {match.group('bracket') or match.group('line') for match in _CASE.finditer(content)}
    return next(iter(values)) if len(values) == 1 else None
