```python
# morph_runtime.errors
class Category(StrEnum):  # AUTH, RATE_LIMIT, SERVER, TIMEOUT, NETWORK, MALFORMED_RESPONSE,
    # VALIDATION, NOT_FOUND, CONFLICT, CONTRACT_DRIFT, NOT_SYNCABLE,
    # CONFIGURATION, UNKNOWN
    ...


class RuntimeFailure(Exception):
    category: Category
    detail: str  # safe to log
    status: int | None
    fields: tuple[str, ...]  # field names for VALIDATION errors


# morph_runtime.http
class HttpClient:
    requests_made: int
    retries_made: int

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str | int] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        """Send one logical request and return the decoded JSON body (None if empty).
        Retries 429/5xx/timeouts/malformed JSON with bounded backoff (a POST is retried only on
        429 or when the connection never opened). Raises RuntimeFailure with a Category; a 404
        raises Category.NOT_FOUND, 401/403 Category.AUTH, 409 Category.CONFLICT, 422/400
        Category.VALIDATION."""


# morph_runtime.paging
def paginate(
    fetch: Callable[[int, int], tuple[list[dict[str, Any]], int | None]],
    *,
    page_size: int,
    max_pages: int = 1000,
) -> Iterator[dict[str, Any]]:
    """fetch(page, page_size) returns (items, total or None). Yields every item once, from page 1."""


# morph_runtime.report
class Outcome(StrEnum):  # CREATED, UPDATED, UNCHANGED, NOT_SYNCABLE, FAILED
    ...


@dataclass(frozen=True)
class RecordResult:
    key: str | None
    outcome: Outcome
    category: Category | None = None
    detail: str = ""
    fields: tuple[str, ...] = ()


@dataclass
class RunReport:
    records: list[RecordResult]
    requests: dict[str, int]

    def add(self, result: RecordResult) -> None: ...
    def fail_run(self, category: Category, detail: str) -> None: ...


# morph_runtime.ops
Record = Mapping[str, str | int | float | bool | None]


class FieldTransformError(Exception):  # a target field could not be computed
    field: str
    detail: str


class MissingFieldError(
    Exception
): ...  # the cause of a FieldTransformError when a source field is absent


# generated modules you may import
# integration.clients:   def source_client() -> HttpClient;  def target_client() -> HttpClient
# integration.transform: def to_target(record: Record) -> dict[str, str | int | float | bool | None]
#     raises FieldTransformError; check `error.__cause__` with isinstance(..., MissingFieldError)
#     the returned dict has one entry per compiled target field
```
