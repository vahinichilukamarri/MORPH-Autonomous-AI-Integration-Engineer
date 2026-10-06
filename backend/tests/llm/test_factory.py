import pytest

from app.llm.base import LLMError
from app.llm.factory import create_llm_provider
from app.llm.groq import GroqProvider
from app.llm.ollama import OllamaProvider
from app.settings import Settings


def test_an_empty_key_is_no_key() -> None:
    assert Settings(groq_api_key="").groq_api_key is None
    assert Settings(groq_api_key="   ").groq_api_key is None
    assert Settings(groq_api_key="gsk_real").groq_api_key is not None


def test_a_missing_key_is_a_clear_error_not_a_crash() -> None:
    with pytest.raises(LLMError, match="GROQ_API_KEY is not set"):
        create_llm_provider(Settings(groq_api_key=None, llm_provider="groq"))


def test_the_factory_builds_the_configured_provider() -> None:
    groq = create_llm_provider(Settings(groq_api_key="gsk_x", groq_model="openai/gpt-oss-20b"))
    assert isinstance(groq, GroqProvider) and groq.model == "openai/gpt-oss-20b"
    ollama = create_llm_provider(Settings(llm_provider="ollama", ollama_model="llama3.1:8b"))
    assert isinstance(ollama, OllamaProvider) and ollama.model == "llama3.1:8b"


def test_settings_never_print_the_key() -> None:
    settings = Settings(groq_api_key="gsk_SECRET_VALUE")
    assert "gsk_SECRET_VALUE" not in repr(settings)
    assert "gsk_SECRET_VALUE" not in str(settings.model_dump())
