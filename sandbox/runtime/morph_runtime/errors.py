"""Failure taxonomy. Every failure the runtime can report belongs to exactly one category."""

from enum import StrEnum


class Category(StrEnum):
    AUTH = "AUTH"
    RATE_LIMIT = "RATE_LIMIT"
    SERVER = "SERVER"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    VALIDATION = "VALIDATION"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    CONTRACT_DRIFT = "CONTRACT_DRIFT"
    NOT_SYNCABLE = "NOT_SYNCABLE"
    CONFIGURATION = "CONFIGURATION"
    UNKNOWN = "UNKNOWN"


class RuntimeFailure(Exception):
    """A categorised failure. ``detail`` is safe to log: it never contains credentials."""

    def __init__(
        self,
        category: Category,
        detail: str,
        *,
        status: int | None = None,
        fields: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"{category.value}: {detail}")
        self.category = category
        self.detail = detail
        self.status = status
        self.fields = fields


def category_for_status(status: int) -> Category:
    if status in (401, 403):
        return Category.AUTH
    if status == 404:
        return Category.NOT_FOUND
    if status == 409:
        return Category.CONFLICT
    if status == 429:
        return Category.RATE_LIMIT
    if status in (400, 422):
        return Category.VALIDATION
    if status >= 500:
        return Category.SERVER
    return Category.UNKNOWN
