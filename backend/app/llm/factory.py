from collections.abc import Callable

from app.llm.base import BaseLLMProvider, LLMError
from app.llm.groq import GroqProvider
from app.llm.ollama import OllamaProvider
from app.settings import Settings


def create_llm_provider(
    settings: Settings, *, on_rate_limit: Callable[[float, int], None] | None = None
) -> BaseLLMProvider:
    """Build the configured real provider. The Groq key comes only from the environment."""
    if settings.llm_provider == "ollama":
        return OllamaProvider(settings.ollama_base_url, settings.ollama_model)
    if settings.groq_api_key is None:
        raise LLMError("GROQ_API_KEY is not set; put it in the gitignored .env or the environment")
    return GroqProvider(
        settings.groq_api_key,
        settings.groq_model,
        timeout_s=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
        wall_clock_budget_s=settings.llm_wall_clock_budget_s,
        reasoning_effort=settings.groq_reasoning_effort,
        default_max_output_tokens=settings.llm_max_output_tokens,
        on_rate_limit=on_rate_limit,
    )
