from __future__ import annotations

import pytest

from bot.config import BotConfig

_REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "t",
    "TELEGRAM_WEBHOOK_SECRET": "w",
    "NYT_API_KEY": "n",
    "ANTHROPIC_API_KEY": "a",
}


def test_from_env_succeeds_without_chat_id(monkeypatch) -> None:
    """Regression test: TELEGRAM_CHAT_ID must NOT be required at startup —
    a first run has no chat ID yet (you get it by messaging /start), so
    requiring it upfront would make the documented setup flow impossible."""
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    for key, val in _REQUIRED.items():
        monkeypatch.setenv(key, val)
    cfg = BotConfig.from_env()
    assert cfg.telegram_chat_id == ""


def test_from_env_picks_up_chat_id_when_set(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    for key, val in _REQUIRED.items():
        monkeypatch.setenv(key, val)
    cfg = BotConfig.from_env()
    assert cfg.telegram_chat_id == "12345"


@pytest.mark.parametrize("missing_key", list(_REQUIRED))
def test_from_env_raises_on_missing_required_var(monkeypatch, missing_key) -> None:
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    for key, val in _REQUIRED.items():
        if key == missing_key:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)
    with pytest.raises(RuntimeError, match=missing_key):
        BotConfig.from_env()
