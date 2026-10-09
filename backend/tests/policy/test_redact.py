"""Redaction: known values (sentinels) and secret-shaped strings; near misses must stay."""

import pytest

from app.policy.redact import REDACTED, Redactor, entropy, high_entropy_secret

SENTINELS = [
    "gsk_SENTINEL0123456789abcdef",
    "approver-sentinel-token-7f3a91",
    "postgresql+psycopg://morph:s3ntinelPassw0rd@localhost:5432/morph",
    "s3ntinelPassw0rd",
]

# secret-shaped strings the Redactor was NOT told about: all must be removed
PATTERNS = [
    "gsk_" + "a1B2c3D4e5F6g7H8i9J0",
    "sk-" + "proj1234567890abcdefXYZ",
    "Bearer " + "abcdEFGH12345678.token",
    "bearer " + "xyzXYZ0123456789",
    "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3",
    "xoxb-" + "1234567890-abcdefghij",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    "postgresql://user:hunter2pass@db.internal:5432/app",
    "https://admin:p4ssw0rd@example.com/path",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
    "Zm9vYmFyMTIzNDU2Nzg5MEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaMDEyMzQ1Njc4OQ",
]

# ordinary text that must survive: identifiers, ids, hashes of the kind we print, prose
NEAR_MISSES = [
    "customer_segment_assignment_identifier_v2",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "the quick brown fox jumps over the lazy dog",
    "skeleton_configuration_for_the_support_user_entity",
    "task-5f0d3c1e-uuid-like-but-short",
    "SupportUserToCrmCustomerTransformationStrategy",
    "http://localhost:8102/users",
    "user@example.com wrote about gsk and sk-short",
]


def test_known_values_are_removed_wherever_they_appear() -> None:
    redactor = Redactor(SENTINELS)
    for value in SENTINELS:
        done = redactor.text(f"before {value} after, and again {value}")
        assert value not in done.text and done.known >= 1 and REDACTED in done.text


def test_known_values_inside_nested_data_and_keys_are_removed() -> None:
    redactor = Redactor(SENTINELS)
    data = {"k": SENTINELS[1], SENTINELS[0]: ["x", {"y": f"v={SENTINELS[3]}"}], "n": 3}
    clean, hits = redactor.value(data)
    assert hits >= 3 and clean["n"] == 3
    assert not any(s in str(clean) for s in SENTINELS)


@pytest.mark.parametrize("value", PATTERNS)
def test_secret_shaped_strings_are_removed_without_being_told(value: str) -> None:
    done = Redactor([]).text(f"x {value} y")
    assert done.patterns >= 1 and done.known == 0
    core = value.splitlines()[1] if value.startswith("-----BEGIN") else value[8:]
    assert core not in done.text


@pytest.mark.parametrize("text", NEAR_MISSES)
def test_ordinary_text_is_left_alone(text: str) -> None:
    done = Redactor([]).text(text)
    assert not done.changed and done.text == text


def test_short_configured_values_are_ignored_so_ordinary_words_survive() -> None:
    redactor = Redactor(["abc", "ok", "user"])
    assert not redactor.text("ok, the support user abc").changed


def test_redaction_is_idempotent() -> None:
    redactor = Redactor(SENTINELS)
    once = redactor.text(" ".join(SENTINELS + PATTERNS)).text
    assert redactor.text(once).text == once


def test_the_long_run_rule_needs_mixed_characters_and_high_entropy() -> None:
    assert not high_entropy_secret("a" * 40)
    assert not high_entropy_secret("customer_segment_assignment_identifier_v2")
    assert high_entropy_secret("Zm9vYmFyMTIzNDU2Nzg5MEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFla")
    assert entropy("aaaa") == 0.0 and entropy("abcd") == 2.0


def test_non_string_values_pass_through() -> None:
    clean, hits = Redactor(SENTINELS).value({"a": 1, "b": None, "c": [True, 2.5]})
    assert clean == {"a": 1, "b": None, "c": [True, 2.5]} and hits == 0


def test_a_nul_byte_is_made_visible_so_it_can_be_stored_and_shown() -> None:
    done = Redactor([]).text("a" + chr(0) + "b")
    assert done.text == "a" + chr(92) + "x00b" and not done.changed
    clean, _ = Redactor([]).value({"k" + chr(0): ["v" + chr(0)]})
    assert chr(0) not in str(clean)
