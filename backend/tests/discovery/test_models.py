import pytest
from pydantic import ValidationError

from app.discovery.models import Constraints, Entity, EntityRole, Field, SystemModel


def make_model() -> SystemModel:
    field = Field(name="id", path="id", json_type="integer", required=True)
    entity = Entity(name="Thing", schema_name="Thing", role=EntityRole.RESOURCE, fields=(field,))
    return SystemModel(
        name="s", api_title="T", api_version="1", spec_hash="0" * 64, entities=(entity,)
    )


def test_models_are_frozen() -> None:
    model = make_model()
    with pytest.raises(ValidationError):
        model.name = "other"  # type: ignore[misc]


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Field.model_validate({"name": "a", "path": "a", "json_type": "string", "surprise": 1})


def test_json_round_trip_is_lossless() -> None:
    model = make_model()
    assert SystemModel.model_validate_json(model.model_dump_json()) == model


def test_constraints_emptiness() -> None:
    assert Constraints().is_empty()
    assert not Constraints(max_length=3).is_empty()


def test_entity_lookup() -> None:
    model = make_model()
    assert model.entity("Thing") is not None
    assert model.entity("Missing") is None
