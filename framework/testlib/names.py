"""Object names that a URL or a listing must encode.

They hold a space, `%`, `+`, `#`, `?`, `&`, `;`, quotes, angle brackets,
brackets, `*`, and letters beyond ASCII; and two need no encoding but are
easily mishandled: a name that begins with a dot, and one of 200
characters. Each sits beside decoys, names that a server or client that
mishandles the encoding would take for its own: `x%41y` for `xAy`, `a+b`
and `a%20b` for `a b`, `hash#tag` for `hash`, and `what?q=1` for `what`.
"""

NAMES: tuple[str, ...] = (
    "plain",
    "a b",
    "a+b",
    "a%20b",
    "xAy",
    "x%41y",
    "hash",
    "hash#tag",
    "what",
    "what?q=1",
    "semi;colon",
    "and&eq=1",
    'it\'s "quoted"',
    "<angle>&amp;",
    "[brackets]",
    "star*",
    "café-日本",
    "~tilde",
    ".hidden",
    "long-" + "x" * 195,
)
