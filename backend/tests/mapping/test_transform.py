from collections.abc import Sequence
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from app.mapping.transform import DatetimeError as DatetimeFormatError
from app.mapping.transform import (
    JsonScalar,
    MissingFieldError,
    Transformation,
    TransformError,
    TypeMismatchError,
    UnmappedValueError,
    execute,
)


def pipeline(*steps: object) -> Transformation:
    return Transformation.model_validate({"steps": list(steps)})


def run(steps: Sequence[object], record: dict[str, JsonScalar]) -> JsonScalar:
    return execute(pipeline(*steps), record)


# ---- source ops --------------------------------------------------------------------------------


def test_copy_passes_values_and_nulls_through() -> None:
    assert run([{"op": "COPY", "field": "a"}], {"a": "x"}) == "x"
    assert run([{"op": "COPY", "field": "a"}], {"a": None}) is None
    assert run([{"op": "COPY", "field": "a"}], {"a": 0}) == 0


def test_a_missing_field_is_a_typed_error() -> None:
    with pytest.raises(MissingFieldError, match="'a'"):
        run([{"op": "COPY", "field": "a"}], {})


def test_join_nonnull_trims_skips_nulls_and_empties() -> None:
    steps = [{"op": "JOIN_NONNULL", "fields": ["first", "last"], "separator": " ", "trim": True}]
    assert run(steps, {"first": " Ravi ", "last": "Iyer "}) == "Ravi Iyer"
    assert run(steps, {"first": "Kiran", "last": None}) == "Kiran"
    assert run(steps, {"first": "Kiran", "last": "  "}) == "Kiran"
    assert run(steps, {"first": None, "last": None}) is None
    keep = [{"op": "JOIN_NONNULL", "fields": ["a", "b"], "separator": "-", "trim": False}]
    assert run(keep, {"a": " x", "b": 5}) == " x-5"


def test_coalesce_takes_the_first_non_null() -> None:
    steps = [{"op": "COALESCE", "fields": ["a", "b", "c"]}]
    assert run(steps, {"a": None, "b": "second", "c": "third"}) == "second"
    assert run(steps, {"a": 0, "b": "second", "c": None}) == 0
    assert run(steps, {"a": None, "b": None, "c": None}) is None


def test_constant_may_be_null() -> None:
    assert run([{"op": "CONSTANT", "value": "X"}], {}) == "X"
    assert run([{"op": "CONSTANT", "value": None}], {}) is None
    assert run([{"op": "CONSTANT", "value": 3}, {"op": "CAST", "to": "str"}], {}) == "3"


# ---- value ops ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("to", "value", "expected"),
    [
        ("int", "1837", 1837),
        ("int", " 42 ", 42),
        ("int", 7.0, 7),
        ("int", 5, 5),
        ("str", 5, "5"),
        ("str", True, "true"),
        ("float", "1.5", 1.5),
        ("float", 2, 2.0),
        ("bool", "TRUE", True),
        ("bool", "0", False),
        ("bool", 1, True),
    ],
)
def test_cast_conversions(to: str, value: JsonScalar, expected: JsonScalar) -> None:
    steps = [{"op": "COPY", "field": "v"}, {"op": "CAST", "to": to}]
    result = run(steps, {"v": value})
    assert result == expected and type(result) is type(expected)


@pytest.mark.parametrize(
    ("to", "value"),
    [
        ("int", "abc"),
        ("int", "1.5"),
        ("int", 1.5),
        ("int", True),
        ("float", "x"),
        ("bool", "maybe"),
    ],
)
def test_cast_failures_are_typed(to: str, value: JsonScalar) -> None:
    with pytest.raises(TypeMismatchError):
        run([{"op": "COPY", "field": "v"}, {"op": "CAST", "to": to}], {"v": value})


def test_cast_null_stays_null() -> None:
    assert run([{"op": "COPY", "field": "v"}, {"op": "CAST", "to": "int"}], {"v": None}) is None


def test_strip_and_add_prefix() -> None:
    strip = [{"op": "COPY", "field": "v"}, {"op": "STRIP_PREFIX", "prefix": "+"}]
    assert run(strip, {"v": "+919876543210"}) == "919876543210"
    assert run(strip, {"v": "919876543210"}) == "919876543210", "no prefix: unchanged"
    assert run(strip, {"v": None}) is None
    add = [{"op": "COPY", "field": "v"}, {"op": "ADD_PREFIX", "prefix": "C-"}]
    assert run(add, {"v": "1837"}) == "C-1837"
    assert run(add, {"v": None}) is None
    with pytest.raises(TypeMismatchError):
        run(add, {"v": 1837})


def test_regex_extract_returns_null_when_there_is_no_match() -> None:
    last = [{"op": "COPY", "field": "n"}, {"op": "REGEX_EXTRACT", "pattern": r"^\S+ (.+)$"}]
    assert run(last, {"n": "Asha Verma"}) == "Verma"
    assert run(last, {"n": "Mary Ann Smith"}) == "Ann Smith"
    assert run(last, {"n": "Priya"}) is None, "no match gives null, not an error"
    assert run(last, {"n": None}) is None
    whole = [{"op": "COPY", "field": "n"}, {"op": "REGEX_EXTRACT", "pattern": r"^C-(\d+)$"}]
    assert run(whole, {"n": "C-1837"}) == "1837"
    group_zero = [
        {"op": "COPY", "field": "n"},
        {"op": "REGEX_EXTRACT", "pattern": r"\d+", "group": 0},
    ]
    assert run(group_zero, {"n": "ab12cd"}) == "12"


def test_regex_replace() -> None:
    steps = [
        {"op": "COPY", "field": "p"},
        {"op": "REGEX_REPLACE", "pattern": r"[^\d]", "replacement": ""},
    ]
    assert run(steps, {"p": "+91 (98765) 43210"}) == "919876543210"
    assert run(steps, {"p": None}) is None


def test_map_enum() -> None:
    mapping = {"ACTIVE": "ENABLED", "SUSPENDED": "BLOCKED"}
    strict = [{"op": "COPY", "field": "s"}, {"op": "MAP_ENUM", "mapping": mapping}]
    assert run(strict, {"s": "ACTIVE"}) == "ENABLED"
    assert run(strict, {"s": None}) is None
    with pytest.raises(UnmappedValueError, match="'GONE'"):
        run(strict, {"s": "GONE"})
    lenient = [
        {"op": "COPY", "field": "s"},
        {"op": "MAP_ENUM", "mapping": mapping, "on_unmapped": "default", "default": "UNKNOWN"},
    ]
    assert run(lenient, {"s": "GONE"}) == "UNKNOWN"
    with pytest.raises(TypeMismatchError):
        run(strict, {"s": 3})


def test_format_datetime_all_directions() -> None:
    iso = "2024-03-05T10:15:30Z"
    to_s = [
        {"op": "COPY", "field": "t"},
        {"op": "FORMAT_DATETIME", "from_format": "iso8601", "to_format": "epoch_s"},
    ]
    assert run(to_s, {"t": iso}) == 1709633730
    assert run(to_s, {"t": "2024-03-05T15:45:30+05:30"}) == 1709633730
    assert run(to_s, {"t": "2024-03-05T10:15:30"}) == 1709633730, "naive means UTC"
    assert run(to_s, {"t": "1970-01-01T00:00:00Z"}) == 0
    assert run(to_s, {"t": None}) is None
    back = [
        {"op": "COPY", "field": "t"},
        {"op": "FORMAT_DATETIME", "from_format": "epoch_s", "to_format": "iso8601"},
    ]
    assert run(back, {"t": 1709633730}) == iso
    ms = [
        {"op": "COPY", "field": "t"},
        {"op": "FORMAT_DATETIME", "from_format": "epoch_ms", "to_format": "iso8601"},
    ]
    assert run(ms, {"t": 1709633730123}) == "2024-03-05T10:15:30.123Z"
    to_ms = [
        {"op": "COPY", "field": "t"},
        {"op": "FORMAT_DATETIME", "from_format": "iso8601", "to_format": "epoch_ms"},
    ]
    assert run(to_ms, {"t": iso}) == 1709633730000


def test_format_datetime_errors_are_typed() -> None:
    steps = [
        {"op": "COPY", "field": "t"},
        {"op": "FORMAT_DATETIME", "from_format": "iso8601", "to_format": "epoch_s"},
    ]
    with pytest.raises(DatetimeFormatError):
        run(steps, {"t": "not a date"})
    with pytest.raises(TypeMismatchError):
        run(steps, {"t": 5})
    epoch = [
        {"op": "COPY", "field": "t"},
        {"op": "FORMAT_DATETIME", "from_format": "epoch_s", "to_format": "iso8601"},
    ]
    with pytest.raises(DatetimeFormatError):
        run(epoch, {"t": "2024"})
    with pytest.raises(DatetimeFormatError):
        run(epoch, {"t": 10**30})


def test_split_part() -> None:
    first = [{"op": "COPY", "field": "n"}, {"op": "SPLIT_PART", "separator": " ", "index": 0}]
    assert run(first, {"n": "Asha Verma"}) == "Asha"
    assert run(first, {"n": "Priya"}) == "Priya"
    last = [{"op": "COPY", "field": "n"}, {"op": "SPLIT_PART", "separator": " ", "index": -1}]
    assert run(last, {"n": "Asha Verma"}) == "Verma"
    second = [{"op": "COPY", "field": "n"}, {"op": "SPLIT_PART", "separator": " ", "index": 1}]
    assert run(second, {"n": "Priya"}) is None, "past the end gives null"
    assert run(second, {"n": None}) is None


def test_steps_chain_in_order() -> None:
    steps = [
        {"op": "COPY", "field": "id"},
        {"op": "REGEX_EXTRACT", "pattern": r"^C-(\d+)$"},
        {"op": "CAST", "to": "int"},
    ]
    assert run(steps, {"id": "C-0042"}) == 42
    assert run(steps, {"id": "bad"}) is None


# ---- shape and validation ----------------------------------------------------------------------


def test_the_first_step_must_be_a_source_op_and_only_the_first() -> None:
    with pytest.raises(ValidationError, match="first step"):
        pipeline({"op": "CAST", "to": "int"})
    with pytest.raises(ValidationError, match="only allowed as the first step"):
        pipeline({"op": "COPY", "field": "a"}, {"op": "COPY", "field": "b"})
    with pytest.raises(ValidationError):
        Transformation.model_validate({"steps": []})


def test_unknown_ops_and_extra_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        pipeline({"op": "EVAL", "code": "1+1"})
    with pytest.raises(ValidationError):
        pipeline({"op": "COPY", "field": "a", "extra": 1})


@pytest.mark.parametrize(
    "pattern",
    ["(", "(a+)+b", "(.*)*", "x" * 201],
)
def test_unsafe_or_invalid_regexes_are_rejected_at_validation(pattern: str) -> None:
    with pytest.raises(ValidationError):
        pipeline(
            {"op": "COPY", "field": "a"}, {"op": "REGEX_EXTRACT", "pattern": pattern, "group": 0}
        )
    with pytest.raises(ValidationError):
        pipeline(
            {"op": "COPY", "field": "a"},
            {"op": "REGEX_REPLACE", "pattern": pattern, "replacement": ""},
        )


def test_regex_group_must_exist() -> None:
    with pytest.raises(ValidationError, match="group 2"):
        pipeline(
            {"op": "COPY", "field": "a"}, {"op": "REGEX_EXTRACT", "pattern": "(a)", "group": 2}
        )


def test_source_fields_property() -> None:
    assert pipeline({"op": "COPY", "field": "a"}).source_fields == ("a",)
    assert pipeline({"op": "JOIN_NONNULL", "fields": ["a", "b"]}).source_fields == ("a", "b")
    assert pipeline({"op": "COALESCE", "fields": ["x", "y"]}).source_fields == ("x", "y")
    assert pipeline({"op": "CONSTANT", "value": 1}).source_fields == ()


def test_pipelines_round_trip_through_json() -> None:
    original = pipeline(
        {"op": "COPY", "field": "status"},
        {"op": "MAP_ENUM", "mapping": {"A": 1, "B": None}},
    )
    assert Transformation.model_validate_json(original.model_dump_json()) == original


# ---- properties --------------------------------------------------------------------------------

scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**18), max_value=10**18),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=40),
)
FIELDS = ["a", "b", "c"]
field_names = st.sampled_from([*FIELDS, "missing"])
short_text = st.text(min_size=1, max_size=6)

source_steps = st.one_of(
    st.builds(lambda f: {"op": "COPY", "field": f}, field_names),
    st.builds(
        lambda fs, sep, trim: {"op": "JOIN_NONNULL", "fields": fs, "separator": sep, "trim": trim},
        st.lists(field_names, min_size=1, max_size=3),
        st.text(max_size=3),
        st.booleans(),
    ),
    st.builds(
        lambda fs: {"op": "COALESCE", "fields": fs}, st.lists(field_names, min_size=1, max_size=3)
    ),
    st.builds(lambda v: {"op": "CONSTANT", "value": v}, scalars),
)
value_steps = st.one_of(
    st.builds(lambda t: {"op": "CAST", "to": t}, st.sampled_from(["int", "str", "float", "bool"])),
    st.builds(lambda p: {"op": "STRIP_PREFIX", "prefix": p}, short_text),
    st.builds(lambda p: {"op": "ADD_PREFIX", "prefix": p}, short_text),
    st.just({"op": "REGEX_EXTRACT", "pattern": r"(\d+)", "group": 1}),
    st.just({"op": "REGEX_EXTRACT", "pattern": r"^\S+ (.+)$", "group": 1}),
    st.just({"op": "REGEX_REPLACE", "pattern": r"\s+", "replacement": "-"}),
    st.builds(
        lambda k, v, d: {"op": "MAP_ENUM", "mapping": {k: v}, "on_unmapped": d, "default": "?"},
        short_text,
        scalars,
        st.sampled_from(["error", "default"]),
    ),
    st.builds(
        lambda f, t: {"op": "FORMAT_DATETIME", "from_format": f, "to_format": t},
        st.sampled_from(["iso8601", "epoch_s", "epoch_ms"]),
        st.sampled_from(["iso8601", "epoch_s", "epoch_ms"]),
    ),
    st.builds(
        lambda s, i: {"op": "SPLIT_PART", "separator": s, "index": i},
        short_text,
        st.integers(min_value=-4, max_value=4),
    ),
)
pipelines = st.builds(
    lambda head, tail: pipeline(head, *tail),
    source_steps,
    st.lists(value_steps, max_size=3),
)
records = st.fixed_dictionaries({name: scalars for name in FIELDS})


@settings(max_examples=300, deadline=None)
@given(pipelines, records)
def test_execution_is_deterministic_and_never_raises_untyped_errors(
    transformation: Transformation, record: dict[str, JsonScalar]
) -> None:
    def attempt() -> tuple[str, Any]:
        try:
            result = execute(transformation, record)
        except TransformError as exc:
            return ("error", type(exc).__name__)
        assert result is None or isinstance(result, str | int | float | bool)
        return ("ok", repr(result))

    assert attempt() == attempt()


@settings(max_examples=150, deadline=None)
@given(pipelines)
def test_every_pipeline_survives_a_json_round_trip(transformation: Transformation) -> None:
    assert Transformation.model_validate_json(transformation.model_dump_json()) == transformation
