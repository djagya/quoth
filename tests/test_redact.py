"""Credential redaction of chat text (used by the episode lanes)."""

from quoth.redact import redact


def test_redact_bearer_and_api_keys():
    out, hits = redact("Authorization: Bearer abcdef1234567890 and api_key = AKIAIOSFODNN7EXAMPLE")
    assert "[REDACTED:bearer]" in out
    assert "[REDACTED:api_key_label]" in out
    assert "abcdef1234567890" not in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert hits >= 2


def test_redact_password_pair():
    out, hits = redact("postgres://user:supersecret@db.example.com:5432/x password: hunter2pass")
    assert "supersecret" not in out
    assert "hunter2pass" not in out
    assert hits >= 1


def test_redact_provider_tokens():
    out, _ = redact("sk-proj-abcdefghijklmnop1234567890 and ghp_AbCdEf0123456789AbCdEf0123456789AB")
    assert "sk-proj-" not in out.replace("sk-proj-[REDACTED", "")  # label prefix kept is fine
    assert "ghp_" not in out or out.count("ghp_") == 0


def test_redact_iban_and_card():
    out, hits = redact("IBAN DE89 3704 0044 0532 0130 00, card 4111 1111 1111 1111")
    assert "3704" not in out
    assert "4111" not in out
    assert hits >= 2


def test_redact_leaves_plain_text_alone():
    text = "Мы решили использовать SQLite с FTS5 и Python 3.13, встреча в 14:30."
    out, hits = redact(text)
    assert out == text
    assert hits == 0


def test_redact_bearer_concatenated_no_whitespace():
    # Regression (review): `Bearer` immediately followed by the token, no space.
    out, hits = redact("header is BearereyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig")
    assert "eyJhbGciOiJI" not in out
    assert "[REDACTED:bearer]" in out
    assert hits >= 1
    # prose stays untouched
    out2, hits2 = redact("the bearer of bad news is here")
    assert out2 == "the bearer of bad news is here"
    assert hits2 == 0


def test_redact_uri_password_with_at():
    # Regression (review): password containing '@' must not leak the userinfo.
    out, hits = redact("postgres://admin:p@ssw0rd99@db.internal:5432/prod")
    assert "p@ssw0rd99" not in out
    assert "admin:" not in out
    assert "[REDACTED:uri_credentials]" in out
    assert hits >= 1
    # multi-@ password: full userinfo redacted up to the host
    out2, _ = redact("redis://u:a@b@c.example.com:6379/0")
    assert "a@b" not in out2 and "u:" not in out2
    # handle-style @ without credentials is untouched
    out3, hits3 = redact("see https://films.example/@user127 for films")
    assert "@user127" in out3
    assert hits3 == 0
