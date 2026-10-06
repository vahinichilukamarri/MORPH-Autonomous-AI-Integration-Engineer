from typing import Annotated

from pydantic import StringConstraints

EMAIL_PATTERN = r"^[^@ ]+@[^@ ]+\.[^@ ]+$"

# A permissive email check that agrees exactly with its published OpenAPI pattern. EmailStr is
# stricter than JSON Schema's `format: email` (it rejects reserved TLDs such as .test), which
# would make the contract promise more than the service accepts.
EmailAddress = Annotated[str, StringConstraints(pattern=EMAIL_PATTERN, max_length=254)]
