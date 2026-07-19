"""Suite-wide hermeticity.

``BotConfig.from_env`` now consults the OS keychain first (bot/secrets.py).
Pin gecko-surf's credential chain to its env backend for every test, so a
developer machine with real `daily-news:*` keychain entries can't leak them
into tests that monkeypatch the environment and expect those values to win
(or expect a missing var to raise).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _pin_credential_chain_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GECKO_CRED_BACKEND", "env")
