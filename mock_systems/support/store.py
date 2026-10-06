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
