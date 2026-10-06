from pydantic import BaseModel, ConfigDict


class Problem(BaseModel):
    model_config = ConfigDict(frozen=True)

    pointer: str
    message: str


class ParseError(Exception):
    """The spec is invalid or uses a construct discovery does not support.

    ``problems`` lists every issue with the JSON pointer where it was found.
    """

    def __init__(self, problems: list[Problem]) -> None:
        self.problems = tuple(problems)
        detail = "; ".join(f"{p.pointer or '/'}: {p.message}" for p in self.problems)
        super().__init__(detail)
