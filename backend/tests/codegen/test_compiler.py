"""Differential tests: compiled field functions must equal the DSL executor, errors included."""

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from morph_runtime.ops import FieldTransformError

from app.codegen.compiler import CompileError, FieldSpec, compile_transform_module, function_name
from app.mapping.transform import (
    AddPrefix,
    Cast,
    Coalesce,
    Constant,
    Copy,
    FormatDatetime,
    JoinNonNull,
    JsonScalar,
    MapEnum,
    RegexExtract,
    RegexReplace,
    SplitPart,
    Step,
    StripPrefix,
    Transformation,
    TransformError,
    execute,
)

FIELDS = ["a", "b", "c", "absent"]
scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(10**12), 10**12),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.sampled_from(
        ["", " ", "C-1837", "C-", "x y z", "  padded  ", "2024-03-05T10:15:30Z", "1709633730"]
    ),
    st.text(max_size=20),
)
records = st.fixed_dictionaries({"a": scalars, "b": scalars, "c": scalars})

source_steps = st.one_of(
    st.builds(Copy, field=st.sampled_from(FIELDS)),
    st.builds(
        JoinNonNull,
        fields=st.lists(st.sampled_from(FIELDS), min_size=1, max_size=3).map(tuple),
        separator=st.sampled_from([" ", ", ", "-"]),
        trim=st.booleans(),
    ),
    st.builds(
        Coalesce, fields=st.lists(st.sampled_from(FIELDS), min_size=1, max_size=3).map(tuple)
    ),
    st.builds(
        Constant, value=st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=8))
    ),
)
value_steps = st.one_of(
    st.builds(Cast, to=st.sampled_from(["int", "str", "float", "bool"])),
    st.builds(StripPrefix, prefix=st.sampled_from(["C-", "x", "20"])),
    st.builds(AddPrefix, prefix=st.sampled_from(["C-", "+", "id:"])),
    st.builds(
        RegexExtract, pattern=st.sampled_from([r"(\d+)", r"^(\w+)\s", r"[a-z]+"]), group=st.just(0)
    ),
    st.builds(
        RegexReplace,
        pattern=st.sampled_from([r"\s+", r"\D", "-"]),
        replacement=st.sampled_from(["", "_"]),
    ),
    st.builds(
        MapEnum,
        mapping=st.just({"ENABLED": "ACTIVE", "x": 1, "C-1837": None}),
        on_unmapped=st.sampled_from(["error", "default"]),
        default=st.sampled_from([None, "OTHER", 0]),
    ),
    st.builds(
        FormatDatetime,
        from_format=st.sampled_from(["iso8601", "epoch_s", "epoch_ms"]),
        to_format=st.sampled_from(["iso8601", "epoch_s", "epoch_ms"]),
    ),
    st.builds(SplitPart, separator=st.sampled_from([" ", "-"]), index=st.integers(-3, 3)),
)
pipelines = st.builds(
    lambda first, rest: Transformation(steps=(first, *rest)),
    source_steps,
    st.lists(value_steps, max_size=4),
)


def load(source: str) -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    exec(compile(source, "<compiled>", "exec"), namespace)  # noqa: S102  (trusted test input)
    return namespace


def outcome_of_executor(t: Transformation, record: dict[str, JsonScalar]) -> tuple[str, Any]:
    try:
        value = execute(t, record)
    except TransformError as error:
        return ("error", type(error).__name__)
    return ("ok", (type(value).__name__, value))


def outcome_of_compiled(
    namespace: dict[str, Any], record: dict[str, JsonScalar]
) -> tuple[str, Any]:
    try:
        value = namespace["to_target"](record)["t"]
    except FieldTransformError as error:
        return ("error", type(error.__cause__).__name__)
    return ("ok", (type(value).__name__, value))


@settings(max_examples=600, deadline=None)
@given(pipelines, records)
def test_compiled_equals_executor(t: Transformation, record: dict[str, JsonScalar]) -> None:
    namespace = load(compile_transform_module([FieldSpec("t", t)]))
    assert outcome_of_compiled(namespace, record) == outcome_of_executor(t, record)


def test_every_op_is_covered_by_the_strategies() -> None:
    covered = {
        Copy, JoinNonNull, Coalesce, Constant, Cast, StripPrefix, AddPrefix, RegexExtract,
        RegexReplace, MapEnum, FormatDatetime, SplitPart,
    }  # fmt: skip
    from typing import get_args

    from app.mapping.transform import SourceStep, ValueStep

    assert covered == set(get_args(SourceStep)) | set(get_args(ValueStep))


def test_module_with_several_fields_and_awkward_names() -> None:
    fields = [
        FieldSpec("customerId", Transformation(steps=(Copy(field="a"), Cast(to="str")))),
        FieldSpec("e-mail address", Transformation(steps=(Constant(value='it\'s "x"\n'),))),
        FieldSpec("status", Transformation(steps=(Copy(field="b"), MapEnum(mapping={"k": "v"})))),
    ]
    source = compile_transform_module(fields)
    ns = load(source)
    assert ns["to_target"]({"a": 5, "b": "k"}) == {
        "customerId": "5",
        "e-mail address": 'it\'s "x"\n',
        "status": "v",
    }
    assert [n for n, _ in ns["TARGET_FIELDS"]] == ["customerId", "e-mail address", "status"]
    assert function_name(1, "e-mail address") == "field_1_e_mail_address"


def test_error_names_the_failing_target_field() -> None:
    fields = [
        FieldSpec("ok", Transformation(steps=(Constant(value=1),))),
        FieldSpec("bad", Transformation(steps=(Copy(field="nope"),))),
    ]
    with pytest.raises(FieldTransformError) as caught:
        load(compile_transform_module(fields))["to_target"]({})
    assert caught.value.field == "bad"


def test_output_is_deterministic_and_has_no_forbidden_constructs() -> None:
    t = Transformation(steps=(Copy(field="a"), RegexReplace(pattern=r"\s+", replacement=" ")))
    first = compile_transform_module([FieldSpec("t", t)])
    assert first == compile_transform_module([FieldSpec("t", t)])
    for banned in ("eval", "exec", "import os", "__import__", "open("):
        assert banned not in first


def test_duplicate_fields_and_non_finite_constants_are_rejected() -> None:
    one = Transformation(steps=(Constant(value=1),))
    with pytest.raises(CompileError):
        compile_transform_module([FieldSpec("x", one), FieldSpec("x", one)])
    with pytest.raises(CompileError):
        compile_transform_module(
            [FieldSpec("x", Transformation(steps=(Constant(value=float("inf")),)))]
        )


def test_strings_with_code_look_alikes_stay_data() -> None:
    nasty = "'); __import__('os').system('x'); ('"
    source = compile_transform_module(
        [FieldSpec("t", Transformation(steps=(Constant(value=nasty),)))]
    )
    assert load(source)["to_target"]({}) == {"t": nasty}
    _ = Step  # the union type is exercised by the pipelines above
