from __future__ import annotations

from gecko.credentials import CredentialError, CredentialRef

from bot.config import BotConfig
from bot.secrets import KEYCHAIN_API, keychain_account, resolve_secret


class _FakeChain:
    """A light fake of gecko-surf's ChainResolver: a dict-backed hit, a
    CredentialError miss."""

    def __init__(self, hits: dict[str, str]) -> None:
        self.hits = hits
        self.asked: list[str] = []

    def resolve(self, ref: CredentialRef) -> str:
        self.asked.append(ref.slot())
        try:
            return self.hits[ref.slot()]
        except KeyError:
            raise CredentialError(f"no credential for {ref.slot()!r}") from None


class _BrokenChain:
    def resolve(self, ref: CredentialRef) -> str:
        raise RuntimeError("keychain exploded")


def test_keychain_account_is_env_name_lowercased() -> None:
    assert keychain_account("NYT_API_KEY") == "nyt_api_key"


def test_keychain_hit_wins_over_env(monkeypatch) -> None:
    monkeypatch.setenv("NYT_API_KEY", "from-env")
    chain = _FakeChain({f"{KEYCHAIN_API}:nyt_api_key": "from-keychain"})
    assert resolve_secret("NYT_API_KEY", resolver=chain) == "from-keychain"
    assert chain.asked == [f"{KEYCHAIN_API}:nyt_api_key"]


def test_keychain_miss_falls_back_to_env(monkeypatch) -> None:
    monkeypatch.setenv("NYT_API_KEY", "from-env")
    assert resolve_secret("NYT_API_KEY", resolver=_FakeChain({})) == "from-env"


def test_broken_keychain_degrades_to_env_never_raises(monkeypatch) -> None:
    """The keychain is a local-dev upgrade, never a new way for the deployed
    bot to fail — ANY chain failure (headless box, locked keychain, missing
    library) must silently fall through to the environment."""
    monkeypatch.setenv("NYT_API_KEY", "from-env")
    assert resolve_secret("NYT_API_KEY", resolver=_BrokenChain()) == "from-env"


def test_nothing_set_returns_empty_string(monkeypatch) -> None:
    monkeypatch.delenv("NYT_API_KEY", raising=False)
    assert resolve_secret("NYT_API_KEY", resolver=_FakeChain({})) == ""


def test_from_env_resolves_through_the_real_gecko_chain(monkeypatch) -> None:
    """End-to-end through gecko-surf's REAL chain (pinned to its env backend
    by conftest, so no OS keychain is touched): the canonical
    GECKO_CRED_DAILY_NEWS_<NAME> slot wins over the plain env var — proving
    from_env consults the chain first and the fallback second."""
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_SECRET",
        "NYT_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.setenv(name, "plain-env")
    monkeypatch.setenv("GECKO_CRED_DAILY_NEWS_NYT_API_KEY", "via-chain")
    cfg = BotConfig.from_env()
    assert cfg.nyt_api_key == "via-chain"
    assert cfg.anthropic_api_key == "plain-env"
