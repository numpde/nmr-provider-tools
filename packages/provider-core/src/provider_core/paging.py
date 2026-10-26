"""Own the two discovery routes' shared limit and opaque cursor wire grammar.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
provider/{jobs,execution_attempts}_list_request.py and their response schemas.
These checks admit representation only; the server owns cursor bindings and
live-page semantics. No traversal, selection or retry policy belongs here.
"""

from provider_core.container_guard import require_container

require_container()

import base64
import binascii
import re

# The API profile caps a discovery page at this item count.
# repo-check: allow-module-constant
MAX_PAGE_ITEMS = 100


def require_cursor(value):
    if value is None:
        return
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("Cursor must be canonical unpadded base64url text or None for the first page")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error):
        raise ValueError("Cursor must be canonical unpadded base64url text") from None
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value:
        raise ValueError("Cursor must be canonical unpadded base64url text")


def paging_query(*, limit, cursor):
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_ITEMS:
        raise ValueError("Page limit must be an integer from 1 through 100")
    require_cursor(cursor)
    query = f"limit={limit}"
    if cursor is not None:
        query += "&cursor=" + cursor
    return query.encode("ascii")
