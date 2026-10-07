"""Page-number pagination with guards against loops and runaway reads."""

import json
from collections.abc import Callable, Iterator
from typing import Any

from morph_runtime.errors import Category, RuntimeFailure

DEFAULT_MAX_PAGES = 1000

# fetch(page, page_size) -> (items on that page, total item count if the API reports one)
FetchPage = Callable[[int, int], tuple[list[dict[str, Any]], int | None]]


def paginate(
    fetch: FetchPage, *, page_size: int, max_pages: int = DEFAULT_MAX_PAGES
) -> Iterator[dict[str, Any]]:
    """Yield every item exactly once, starting from page 1.

    Stops on an empty page, on a short page, or once the reported total has been read. A page that
    repeats the previous one means the server ignores the page parameter: that is reported as
    contract drift instead of looping.
    """
    if page_size < 1:
        raise RuntimeFailure(Category.CONFIGURATION, "page_size must be at least 1")
    seen = 0
    previous: str | None = None
    for page in range(1, max_pages + 1):
        items, total = fetch(page, page_size)
        if not items:
            return
        fingerprint = json.dumps(items, sort_keys=True, default=str)
        if fingerprint == previous:
            raise RuntimeFailure(
                Category.CONTRACT_DRIFT, f"page {page} repeats page {page - 1}; paging is stuck"
            )
        previous = fingerprint
        yield from items
        seen += len(items)
        if len(items) < page_size or (total is not None and seen >= total):
            return
    raise RuntimeFailure(Category.CONTRACT_DRIFT, f"more than {max_pages} pages; giving up")
