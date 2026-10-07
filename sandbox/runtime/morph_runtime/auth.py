"""Authentication schemes. Secrets stay inside the auth object and never appear in ``repr``."""

from dataclasses import dataclass, field
from typing import Protocol


class Auth(Protocol):
    @property
    def scheme(self) -> str: ...

    def headers(self) -> dict[str, str]: ...


@dataclass(frozen=True)
class ApiKeyAuth:
    header: str
    key: str = field(repr=False)
    scheme: str = "apiKey"

    def headers(self) -> dict[str, str]:
        return {self.header: self.key}


@dataclass(frozen=True)
class BearerAuth:
    token: str = field(repr=False)
    scheme: str = "bearer"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}
