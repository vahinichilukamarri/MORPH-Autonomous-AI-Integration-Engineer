from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from morph_runtime.errors import Category, RuntimeFailure
from morph_runtime.paging import paginate
from morph_runtime.report import (
    EXIT_FATAL,
    EXIT_OK,
    EXIT_PARTIAL,
    Outcome,
    RecordResult,
    RunReport,
)


def source(n: int, *, report_total: bool = True) -> Any:
    rows = [{"id": i} for i in range(n)]
    calls: list[int] = []

    def fetch(page: int, size: int) -> tuple[list[dict[str, Any]], int | None]:
        calls.append(page)
        return rows[(page - 1) * size : page * size], (n if report_total else None)

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


@given(n=st.integers(0, 300), size=st.integers(1, 120), total=st.booleans())
def test_every_item_exactly_once(n: int, size: int, total: bool) -> None:
    got = list(paginate(source(n, report_total=total), page_size=size))
    assert [r["id"] for r in got] == list(range(n))


@pytest.mark.parametrize("n", [0, 1, 99, 100, 101, 250])
def test_no_extra_request_when_total_known(n: int) -> None:
    fetch = source(n)
    list(paginate(fetch, page_size=100))
    assert len(fetch.calls) == max(1, -(-n // 100))


def test_stuck_paging_is_contract_drift() -> None:
    def fetch(page: int, size: int) -> tuple[list[dict[str, Any]], int | None]:
        return [{"id": 1}, {"id": 2}], None

    with pytest.raises(RuntimeFailure) as caught:
        list(paginate(fetch, page_size=2))
    assert caught.value.category is Category.CONTRACT_DRIFT


def test_max_pages_guard() -> None:
    def fetch(page: int, size: int) -> tuple[list[dict[str, Any]], int | None]:
        return [{"id": page}], None

    with pytest.raises(RuntimeFailure) as caught:
        list(paginate(fetch, page_size=1, max_pages=5))
    assert caught.value.category is Category.CONTRACT_DRIFT


def test_bad_page_size() -> None:
    with pytest.raises(RuntimeFailure):
        list(paginate(source(1), page_size=0))


def test_report_status_and_exit_codes() -> None:
    report = RunReport()
    assert (report.status.value, report.exit_code) == ("OK", EXIT_OK)
    report.add(RecordResult("a", Outcome.CREATED))
    report.add(RecordResult("b", Outcome.NOT_SYNCABLE, Category.NOT_SYNCABLE, "null id"))
    assert report.exit_code == EXIT_OK
    report.add(RecordResult("c", Outcome.FAILED, Category.VALIDATION, "422", ("email",)))
    assert report.status.value == "PARTIAL" and report.exit_code == EXIT_PARTIAL
    report.fail_run(Category.AUTH, "401 from target")
    assert report.status.value == "FATAL" and report.exit_code == EXIT_FATAL
    data = report.to_dict()
    assert data["counts"]["CREATED"] == 1 and data["fatal"]["category"] == "AUTH"
    assert data["records"][2]["fields"] == ["email"]
