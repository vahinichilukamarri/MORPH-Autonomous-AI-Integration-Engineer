from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    model_validator,
)

from common.types import EmailAddress

CUSTOMER_ID_PATTERN = r"^C-\d+$"
E164_PATTERN = r"^\+[1-9]\d{7,14}$"


class CustomerStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    SUSPENDED = "SUSPENDED"


class CustomerSegment(StrEnum):
    SMB = "SMB"
    MIDMARKET = "MIDMARKET"
    ENTERPRISE = "ENTERPRISE"


FirstName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=100,
        description="Given name of the customer. Required.",
        examples=["Asha"],
    ),
]
LastName = Annotated[
    str | None,
    Field(
        max_length=100,
        description="Family name of the customer. Null when the customer has no recorded surname.",
        examples=["Verma"],
    ),
]
Email = Annotated[
    EmailAddress,
    Field(
        description="Primary contact email address. Required.",
        examples=["asha.verma@example.com"],
    ),
]
Phone = Annotated[
    str | None,
    Field(
        pattern=E164_PATTERN,
        description="Contact phone number in E.164 format (leading '+', country code, digits "
        "only). Null when unknown.",
        examples=["+919876543210"],
    ),
]
Status = Annotated[
    CustomerStatus,
    Field(description="Lifecycle status of the customer account.", examples=["ACTIVE"]),
]
Segment = Annotated[
    CustomerSegment,
    Field(description="Commercial segment the customer belongs to.", examples=["MIDMARKET"]),
]


class Customer(BaseModel):
    customer_id: str = Field(
        pattern=CUSTOMER_ID_PATTERN,
        description="Unique customer identifier: the letter 'C', a hyphen and decimal digits.",
        examples=["C-1837"],
    )
    first_name: FirstName
    last_name: LastName
    email: Email
    phone: Phone
    status: Status
    segment: Segment
    created_at: AwareDatetime = Field(
        description="Creation time as an ISO-8601 UTC datetime string ending in 'Z'.",
        examples=["2024-03-05T10:15:30Z"],
    )

    @field_serializer("created_at")
    def _serialize_created_at(self, value: datetime) -> str:
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class CustomerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: FirstName
    last_name: LastName = None
    email: Email
    phone: Phone = None
    status: Status = CustomerStatus.ACTIVE
    segment: Segment


_NOT_NULLABLE_ON_PATCH = ("first_name", "email", "status", "segment")


def _forbid_null_in_schema(schema: dict[str, Any]) -> None:
    """Optional on PATCH, but explicit null is rejected: say so in the OpenAPI schema."""
    for name in _NOT_NULLABLE_ON_PATCH:
        prop = schema["properties"][name]
        options = [o for o in prop.pop("anyOf", []) if o != {"type": "null"}]
        if len(options) == 1:
            prop.update(options[0])


class CustomerPatch(BaseModel):
    """Partial update. Only the fields present in the request body are changed."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_forbid_null_in_schema)

    first_name: FirstName | None = None
    last_name: LastName = None
    email: Email | None = None
    phone: Phone = None
    status: Status | None = None
    segment: Segment | None = None

    @model_validator(mode="after")
    def _reject_null_for_required_fields(self) -> "CustomerPatch":
        for name in _NOT_NULLABLE_ON_PATCH:
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be set to null")
        return self


class CustomerPage(BaseModel):
    items: list[Customer] = Field(description="Customers on the requested page.")
    page: int = Field(ge=1, description="1-based page number that was returned.", examples=[1])
    page_size: int = Field(ge=1, description="Maximum number of items per page.", examples=[20])
    total: int = Field(
        ge=0, description="Total number of customers across all pages.", examples=[50]
    )
