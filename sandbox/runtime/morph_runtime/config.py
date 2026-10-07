"""Environment access for generated code. Only ``MORPH_*`` variables are readable."""

import os

from morph_runtime.auth import Auth
from morph_runtime.errors import Category, RuntimeFailure
from morph_runtime.http import HttpClient
from morph_runtime.retry import RetryPolicy

PREFIX = "MORPH_"


def env(name: str) -> str:
    """A required ``MORPH_*`` variable."""
    if not name.startswith(PREFIX):
        raise RuntimeFailure(Category.CONFIGURATION, f"{name} is not a MORPH_* variable")
    value = os.environ.get(name)
    if not value:
        raise RuntimeFailure(Category.CONFIGURATION, f"environment variable {name} is not set")
    return value


def env_optional(name: str, default: str) -> str:
    if not name.startswith(PREFIX):
        raise RuntimeFailure(Category.CONFIGURATION, f"{name} is not a MORPH_* variable")
    return os.environ.get(name) or default


def make_client(url_var: str, auth: Auth) -> HttpClient:
    """An HTTP client for the system whose base URL is in ``url_var``."""
    timeout = float(env_optional("MORPH_HTTP_TIMEOUT_S", "5"))
    return HttpClient(env(url_var), auth, timeout=timeout, policy=RetryPolicy(total_budget=12.0))
