"""The secret values this deployment knows about, for redaction. Values are never printed."""

from app.settings import Settings


def known_secrets(settings: Settings) -> tuple[str, ...]:
    held = [settings.groq_api_key, settings.approver_token]
    found = [v.get_secret_value() for v in held if v is not None]
    url = settings.database_url
    password = url.split("://", 1)[-1].split("@", 1)[0].partition(":")[2]
    return tuple(v for v in [*found, password, url] if v)
