"""Migration 0006: the full usage block and total_tokens are stored per call, nullable."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import LLMCall


def _call(**changes: object) -> LLMCall:
    base: dict[str, object] = {
        "attempt": 1, "provider": "groq", "model": "m", "prompt_hash": "h" * 64,
        "latency_ms": 5, "outcome": "OK", "source": "network", "http_attempts": 1,
    }  # fmt: skip
    return LLMCall(**{**base, **changes})


def test_usage_block_and_total_tokens_round_trip(session: Session) -> None:
    block = {
        "prompt_tokens": 120,
        "completion_tokens": 40,
        "total_tokens": 160,
        "completion_tokens_details": {"reasoning_tokens": 25},
    }
    session.add(_call(total_tokens=160, usage=block))
    session.flush()
    session.expire_all()
    row = session.scalars(select(LLMCall)).one()
    assert row.total_tokens == 160 and row.usage == block


def test_both_columns_are_optional(session: Session) -> None:
    session.add(_call())
    session.flush()
    row = session.scalars(select(LLMCall)).one()
    assert row.total_tokens is None and row.usage is None
