import builtins
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from crm.models import Customer, CustomerCreate, CustomerPatch, CustomerSegment, CustomerStatus

SEED = 42
SEED_COUNT = 50
FIRST_NEW_ID = 2000

_FIRST_NAMES = [
    "Asha", "Ravi", "Meera", "Kiran", "Priya", "Arjun", "Neha", "Rohan", "Isha", "Vikram",
    "Anita", "Sanjay", "Divya", "Karan", "Pooja", "Manoj", "Sneha", "Amit", "Lakshmi", "Nikhil",
]  # fmt: skip
_LAST_NAMES = [
    "Verma", "Iyer", "Nair", "Reddy", "Sharma", "Gupta", "Menon", "Patel", "Rao", "Kapoor",
    "Bose", "Das", "Joshi", "Mehta", "Singh",
]  # fmt: skip
_SEED_EPOCH = datetime(2023, 1, 1, tzinfo=UTC)


def _seed_customers() -> list[Customer]:
    rng = random.Random(SEED)
    ids = rng.sample(range(1000, FIRST_NEW_ID), SEED_COUNT)
    customers: list[Customer] = []
    for n in ids:
        first = rng.choice(_FIRST_NAMES)
        last = rng.choice(_LAST_NAMES) if rng.random() > 0.15 else None
        handle = f"{first}.{last}".lower() if last else first.lower()
        phone = f"+91{rng.randint(6_000_000_000, 9_999_999_999)}" if rng.random() > 0.2 else None
        customers.append(
            Customer(
                customer_id=f"C-{n}",
                first_name=first,
                last_name=last,
                email=f"{handle}{n}@example.com",
                phone=phone,
                status=rng.choices(list(CustomerStatus), weights=[6, 3, 1])[0],
                segment=rng.choices(list(CustomerSegment), weights=[5, 3, 2])[0],
                created_at=_SEED_EPOCH + timedelta(seconds=rng.randint(0, 600 * 86_400)),
            )
        )
    return customers


Records = builtins.list[dict[str, Any]]


class CustomerStore:
    def __init__(self) -> None:
        self._customers: dict[str, Customer] = {}
        self._next_id = FIRST_NEW_ID
        self.reset()

    def reset(self) -> None:
        self._customers = {c.customer_id: c for c in _seed_customers()}
        self._next_id = FIRST_NEW_ID

    def list(self, page: int, page_size: int) -> tuple[list[Customer], int]:
        values = list(self._customers.values())
        start = (page - 1) * page_size
        return values[start : start + page_size], len(values)

    def get(self, customer_id: str) -> Customer | None:
        return self._customers.get(customer_id)

    def create(self, data: CustomerCreate) -> Customer:
        customer = Customer(
            customer_id=f"C-{self._next_id}",
            created_at=datetime.now(UTC).replace(microsecond=0),
            **data.model_dump(),
        )
        self._next_id += 1
        self._customers[customer.customer_id] = customer
        return customer

    def patch(self, customer_id: str, data: CustomerPatch) -> Customer | None:
        current = self._customers.get(customer_id)
        if current is None:
            return None
        changes = data.model_dump(exclude_unset=True)
        updated = Customer.model_validate(current.model_dump() | changes)
        self._customers[customer_id] = updated
        return updated

    def dump(self) -> Records:
        return [c.model_dump(mode="json") for c in self._customers.values()]

    def load(self, records: Records) -> None:
        """Replace all customers (admin only). New ids continue after the highest loaded id."""
        try:
            customers = [Customer.model_validate(r) for r in records]
        except ValidationError as error:
            raise ValueError(f"invalid customer record: {error.error_count()} error(s)") from error
        if len({c.customer_id for c in customers}) != len(customers):
            raise ValueError("duplicate customer_id")
        self._customers = {c.customer_id: c for c in customers}
        numbers = [int(c.customer_id[2:]) for c in customers if c.customer_id[2:].isdigit()]
        self._next_id = max([FIRST_NEW_ID - 1, *numbers]) + 1
