from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, Field, JsonValue, model_validator

from app.discovery.models import AuthScheme, EntityRole


class SpecSource(BaseModel):
    file: str | None = Field(
        default=None, description="Path to a JSON OpenAPI file under the configured spec root."
    )
    url: str | None = Field(default=None, description="http(s) URL of a live /openapi.json.")

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.file is None) == (self.url is None):
            raise ValueError("give exactly one of 'file' or 'url'")
        return self


class IngestRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="Stable system name.")
    source: SpecSource


class IngestResponse(BaseModel):
    system_id: int
    name: str
    version: int
    version_id: int
    created: bool = Field(description="False when this exact spec was already ingested.")
    spec_hash: str
    entities: int
    fields: int
    fields_embedded: int
    entities_embedded: int
    embedding_model: str


class ProblemOut(BaseModel):
    pointer: str
    message: str


class InvalidSpecResponse(BaseModel):
    error: str = "invalid_spec"
    problems: list[ProblemOut]


class VersionSummary(BaseModel):
    version: int
    version_id: int
    api_title: str
    api_version: str
    spec_hash: str
    ingested_at: datetime
    entities: int


class SystemSummary(BaseModel):
    id: int
    name: str
    latest_version: int | None
    api_title: str | None
    api_version: str | None


class SystemDetail(SystemSummary):
    version_id: int
    spec_hash: str
    ingested_at: datetime
    entities: int
    fields: int
    operations: int
    auth_schemes: list[AuthScheme]


class FieldOut(BaseModel):
    id: int
    path: str
    json_type: str
    format: str | None
    nullable: bool
    required: bool
    enum_values: list[JsonValue]
    description: str | None
    examples: list[JsonValue]
    constraints: dict[str, Any]
    item_type: str | None
    entity_ref: str | None


class EntityOut(BaseModel):
    id: int
    name: str
    role: EntityRole
    description: str | None
    wrapped_entity: str | None
    fields: list[FieldOut]


class SimilarFieldOut(BaseModel):
    field_id: int
    system_id: int
    system_name: str
    version: int
    entity: str
    entity_role: EntityRole
    path: str
    json_type: str
    distance: float
