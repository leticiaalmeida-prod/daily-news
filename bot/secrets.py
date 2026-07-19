"""Secret resolution: OS keychain first (gecko-surf's own credential chain),
environment as fallback.

The pattern, and why
--------------------
In production (Vercel) secrets are real Environment Variables — that stays
exactly as it is, nothing to change there. Locally, though, a plaintext
``.env`` file is the *leakiest* place a secret can live (any process can read
it, it's one careless ``git add -f`` away from a commit). gecko-surf already
ships a credential chain (``gecko auth set`` / the ``keyring`` library) that
stores secrets in the OS keychain — encrypted at rest, unlocked with the
login session — so this module lets local dev secrets live there instead,
with the environment (which is what both Vercel env vars AND a local ``.env``
resolve to, via ``bot/config.py``'s ``load_dotenv``) as the fallback.

Store a secret locally with gecko-surf's CLI, one per config name::

    gecko auth set daily_news --account nyt_api_key
    gecko auth set daily_news --account telegram_bot_token
    ...

The account name is just the env var name, lowercased. Nothing else changes:
a machine with no keychain entry (Vercel, CI, a fresh laptop) falls through
to the environment silently — resolution failure here is a miss, never a
crash, and no code path ever logs a secret value.
"""

from __future__ import annotations

import os
from typing import Any

# The keychain namespace for this app: entries live at `daily_news:<account>`
# (gecko-surf prefixes its own `gecko:` service name on top).
KEYCHAIN_API = "daily_news"


def keychain_account(env_name: str) -> str:
    """The keychain account slot for a config name: the env var, lowercased —
    one obvious mapping, no second naming scheme to remember."""
    return env_name.lower()


def resolve_secret(env_name: str, *, resolver: Any | None = None) -> str:
    """The secret for ``env_name``: OS keychain (via gecko-surf's chain)
    first, then the environment. Returns ``""`` when nothing is set —
    ``BotConfig.from_env`` turns that into the clear missing-variable error.

    ``resolver`` is injectable for tests; ``None`` builds gecko-surf's
    default chain (keyring -> command hook -> ``GECKO_CRED_*`` env). ANY
    failure inside the chain — no keychain on a headless box, a locked
    keychain, the library missing — degrades to the environment rather than
    raising: the keychain is an upgrade for local dev, never a new way for
    the deployed bot to fail.
    """
    try:
        from gecko.credentials import CredentialRef, default_resolver

        chain = resolver if resolver is not None else default_resolver()
        value = chain.resolve(
            CredentialRef(api=KEYCHAIN_API, account=keychain_account(env_name))
        )
        if value:
            return value
    except Exception:  # noqa: BLE001 - a keychain miss/absence is a fallthrough, not an error
        pass
    return os.environ.get(env_name, "")
