from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictStr,
)

from common.types import EmailAddress

PHONE_DIGITS_PATTERN = r"^[1-9]\d{7,14}$"
# Words separated by exactly one space, no leading or trailing space.
FULL_NAME_PATTERN = r"^[^ ]+( [^ ]+)*$"


def _integral_float_to_int(value: object) -> object:
    """JSON Schema treats 182.0 as an integer; any other coercion (e.g. strings) stays rejected."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


class AccountState(StrEnum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    BLOCKED = "BLOCKED"


class Tier(StrEnum):
    STANDARD = "STANDARD"
    PRIORITY = "PRIORITY"


class User(BaseModel):
    model_config = ConfigDict(extra="forbid")

    userId: Annotated[
        int,
        Field(
            strict=True,
            ge=1,
            description="Numeric user identifier. For users imported from the CRM this is the "
            "numeric part of the CRM customer id (C-1837 -> 1837).",
            examples=[1837],
        ),
        BeforeValidator(_integral_float_to_int),
    ]
    externalRef: Annotated[
        StrictStr | None,
        Field(
            description="Reference to the originating system record, e.g. the original CRM "
            "customer id. Null when the user was not imported.",
            examples=["C-1837"],
        ),
    ] = None
    fullName: Annotated[
        StrictStr,
        Field(
            min_length=1,
            max_length=200,
            pattern=FULL_NAME_PATTERN,
            description="Display name: first and last name joined by a single space, trimmed. "
            "Only the first name when the person has no last name.",
            examples=["Asha Verma"],
        ),
    ]
    email_address: Annotated[
        EmailAddress,
        Field(description="Contact email address.", examples=["asha.verma@example.com"]),
    ]
    phoneNumber: Annotated[
        StrictStr | None,
        Field(
            pattern=PHONE_DIGITS_PATTERN,
            description="Phone number as digits only: country code and number, no '+', no "
            "separators. Null when unknown.",
            examples=["919876543210"],
        ),
    ] = None
    accountState: Annotated[
        AccountState,
        Field(description="Whether the user may use the support portal.", examples=["ENABLED"]),
    ]
    tier: Annotated[
        Tier,
        Field(description="Support service tier.", examples=["STANDARD"]),
    ]
    createdAt: Annotated[
        int,
        Field(
            strict=True,
            ge=0,
            description="Creation time as an integer count of seconds since the Unix epoch (UTC).",
            examples=[1709633730],
        ),
        BeforeValidator(_integral_float_to_int),
    ]


class ErrorDetail(BaseModel):
    location: str = Field(
        description="Where the problem is: body, path or query.", examples=["body"]
    )
    field: str = Field(description="Name of the offending field.", examples=["phoneNumber"])
    expected: str = Field(description="What the field must satisfy.")
    received: object = Field(description="The value that was received (null when missing).")


class ErrorBody(BaseModel):
    code: str = Field(examples=["VALIDATION_ERROR"])
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorBody
