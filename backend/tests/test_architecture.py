"""Architecture rules enforced as tests (engineering rules 9 and 10)."""

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
FORBIDDEN = re.compile(
    r"answer_key|answer-key|morph_bench|bench[/\\]|references[/\\]", re.IGNORECASE
)


def test_the_backend_never_touches_answer_keys_bench_or_reference_pipelines() -> None:
    offenders: list[str] = []
    for path in APP.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".md"}:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(APP)}:{number}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)


def test_llm_calls_only_happen_inside_the_provider_layer() -> None:
    """Only app/llm talks HTTP to model APIs; everything else goes through its providers."""
    for path in APP.rglob("*.py"):
        relative = path.relative_to(APP).as_posix()
        text = path.read_text(encoding="utf-8")
        if relative.startswith("llm/"):
            continue
        assert "api.groq.com" not in text, relative
        assert "chat/completions" not in text, relative
        assert "/api/chat" not in text, relative
