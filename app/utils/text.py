"""Text helpers shared across providers."""

from __future__ import annotations


def is_masked(text: str) -> bool:
    """True for LinkedIn's redaction of a value, e.g. ``"************ ******"``.

    LinkedIn substitutes asterisks for values it will not show a given viewer,
    and it does so inconsistently — the same field can arrive intact on one
    request and redacted on the next. A redaction is *missing data*, not a
    value: letting one through would put ``"******"`` in the API's output, which
    is worse than admitting the field is unknown. So every provider that can
    encounter one filters it to ``None``.

    Deliberately strict about what counts: a string is a redaction only if every
    non-space character is an asterisk. Real values containing asterisks
    (``"5 * 3 Consulting"``) are left alone.
    """
    visible = [char for char in text if not char.isspace()]
    return bool(visible) and all(char == "*" for char in visible)
