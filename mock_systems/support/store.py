from typing import Any

from pydantic import ValidationError

from support.models import User


class UserStore:
    def __init__(self) -> None:
        self._users: dict[int, User] = {}

    def reset(self) -> None:
        self._users = {}

    def get(self, user_id: int) -> User | None:
        return self._users.get(user_id)

    def create(self, user: User) -> bool:
        """Insert a user. Returns False when the userId already exists."""
        if user.userId in self._users:
            return False
        self._users[user.userId] = user
        return True

    def upsert(self, user: User) -> bool:
        """Insert or replace a user. Returns True when a new user was created."""
        created = user.userId not in self._users
        self._users[user.userId] = user
        return created

    def dump(self) -> list[dict[str, Any]]:
        return [u.model_dump(mode="json") for u in self._users.values()]

    def load(self, records: list[dict[str, Any]]) -> None:
        """Replace all users (admin only)."""
        try:
            users = [User.model_validate(r) for r in records]
        except ValidationError as error:
            raise ValueError(f"invalid user record: {error.error_count()} error(s)") from error
        if len({u.userId for u in users}) != len(users):
            raise ValueError("duplicate userId")
        self._users = {u.userId: u for u in users}
