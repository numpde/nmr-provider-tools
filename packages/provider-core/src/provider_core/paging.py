"""Own the two discovery routes' shared limit and opaque cursor wire grammar.

Authority: nmr-api-v1 6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
provider/{jobs,execution_attempts}_list_request.py and their response schemas.
These checks admit representation only; the server owns cursor bindings and
live-page semantics. No traversal, selection or retry policy belongs here.
"""

from provider_core.container_guard import require_container

require_container()

import base64
import re

from provider_core.type_requirements import require_type

# The API profile caps a discovery page at this item count.
# repo-check: allow-module-constant
MAX_PAGE_ITEMS = 100


def require_cursor(cursor):
    if cursor is None:
        return

    require_type(cursor, str)

    if not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
        raise ValueError(f"{cursor=} should be nonempty unpadded base64url")

    decoded_cursor = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    canonical_cursor = base64.urlsafe_b64encode(decoded_cursor).decode("ascii").rstrip("=")

    if not (cursor == canonical_cursor):
        raise ValueError(f"{cursor=} should match {canonical_cursor=}")


def paging_query(*, limit, cursor):
    require_type(limit, int)

    if not (1 <= limit <= MAX_PAGE_ITEMS):
        raise ValueError(f"{limit=} should be between 1 and {MAX_PAGE_ITEMS=}")

    require_cursor(cursor)
    query = f"limit={limit}"

    if cursor is not None:
        query += "&cursor=" + cursor

    return query.encode("ascii")
