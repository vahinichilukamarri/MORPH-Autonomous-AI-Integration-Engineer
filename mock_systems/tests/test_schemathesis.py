"""Property-based conformance of both mock systems to their own OpenAPI contracts."""

from typing import Any

import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.specs.openapi.checks import (
    allow_header_conformance,
    negative_data_rejection,
    positive_data_acceptance,
    unsupported_method,
)

from crm.main import create_app as create_crm_app
from support.main import create_app as create_support_app

CRM_HEADERS = {"X-API-Key": "fuzz-key"}
SUPPORT_HEADERS = {"Authorization": "Bearer fuzz-token"}

crm_schema = schemathesis.openapi.from_asgi(
    "/openapi.json", create_crm_app(api_key="fuzz-key", admin_token="fuzz-admin")
)
support_schema = schemathesis.openapi.from_asgi(
    "/openapi.json", create_support_app(token="fuzz-token", admin_token="fuzz-admin")
)

# The framework's 405 `Allow` header is not part of the contract under test.
EXCLUDED = [unsupported_method, allow_header_conformance]
# PUT /users/{userId} deliberately rejects a body whose userId differs from the path; an OpenAPI
# schema cannot express that cross-field rule, so independently generated values are rejected.
# The CRM is lenient by design: it ignores unknown query parameters.
EXCLUDED_FOR_CRM = [*EXCLUDED, negative_data_rejection]
EXCLUDED_FOR_PUT = [*EXCLUDED, positive_data_acceptance]

fuzz = settings(max_examples=50, deadline=None, suppress_health_check=list(HealthCheck))


@crm_schema.parametrize()
@fuzz
def test_crm_conforms_to_openapi(case: schemathesis.Case[Any]) -> None:
    case.call_and_validate(headers=CRM_HEADERS, excluded_checks=EXCLUDED_FOR_CRM)


@support_schema.parametrize()
@fuzz
def test_support_conforms_to_openapi(case: schemathesis.Case[Any]) -> None:
    excluded = EXCLUDED_FOR_PUT if case.method == "PUT" else EXCLUDED
    case.call_and_validate(headers=SUPPORT_HEADERS, excluded_checks=excluded)
