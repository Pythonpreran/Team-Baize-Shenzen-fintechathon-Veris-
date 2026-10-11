"""Configuration and secret loading.

ONE rule, and it is not negotiable: the API key lives in the environment or in a
`.env` file that is gitignored. It is never in source, never in the front end, and
never in an API response. `ai_status()` reports whether a key is *present*; it never
reports the key.

SECOND rule: every AI feature degrades. A missing key must not break the product.
This matters for three separate reasons and all three are real:

  * A judge cloning the repo will not have our key, and a demo that 500s for them is
    worse than a demo with no AI.
  * The 158 existing tests must not require a network round trip or a paid call.
  * If the rate limit or the network dies mid-demo, the deterministic engine is still
    the source of truth for every consequential number — so the product should keep
    working with a visible, honest "AI unavailable" state rather than collapsing.

So `ai_enabled()` is checked at every entry point, and each AI capability has a
deterministic fallback that is labelled as such in the response.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import llm

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"

# Retrieval has a fixed local chunk cap and every model prompt has a separately enforced
# token budget in llm.py. No raw document embedding request is made from this module.
MAX_CHUNKS_TO_EMBED = 400
MAX_CONTEXT_CHUNKS = 6


def _load_env_file(path: Path = ENV_FILE) -> None:
    """Read a simple KEY=VALUE .env into os.environ without overwriting real env
    vars. Deliberately not python-dotenv: this is twelve lines and one less
    dependency to install on demo night."""
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        return


_load_env_file()


def api_key() -> str | None:
    """The selected provider key, or None. Never logged or returned by an endpoint."""
    return llm.settings().api_key


def live_sources_enabled() -> bool:
    """Whether the external agents may make real outbound HTTP calls.

    OFF by default, and deliberately so: the test suite must never require a network
    round trip, and a build that silently hit government APIs on every request would be
    both slow and impossible to run offline. The demo `.env` sets
    `PROVEN_LIVE_SOURCES=1`, which flips Sentinel, Discovery and the drift index onto
    real data. When it is off, every source falls back to a labelled DEMO_FIXTURE and
    says so — the one thing that must never happen is a fixture rendered as live.
    """
    val = (os.getenv("PROVEN_LIVE_SOURCES") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def demo_site_url() -> str:
    """Where the demo business's public website is served from.

    Hock Lee Foods is fictional, so `hockleefoods.com.sg` does not resolve and every live
    Discovery run against it died with `getaddrinfo failed`. The agent was reaching for a
    page that could not exist, which meant the one capability it exists to demonstrate —
    noticing a change the business published but never told its insurer — could not be
    shown at all.

    So the repo ships that website as a separate process (`demo-site/site.py`, port 8100)
    and this points at it. What the product does to it is a genuine outbound HTTP GET:
    real request, real HTML, real parsing, real grounding. Only the company is a stand-in.

    Overridable, and that matters twice. A real deployment points this at the customer's
    actual URL and nothing else changes. A test points it at a local stub and never opens
    a socket it did not create.
    """
    return (os.getenv("PROVEN_DEMO_SITE_URL") or "http://127.0.0.1:8100").strip()


def smtp_settings() -> dict:
    """Mail server settings from the environment.

    Same rule as the API key: read from the environment, never from source, and never
    returned by an endpoint. `mailer.status()` reports whether a password is PRESENT and
    never what it is.
    """
    return {
        "host": (os.getenv("PROVEN_SMTP_HOST") or "").strip(),
        "port": int((os.getenv("PROVEN_SMTP_PORT") or "587").strip() or 587),
        "user": (os.getenv("PROVEN_SMTP_USER") or "").strip(),
        "password": (os.getenv("PROVEN_SMTP_PASSWORD") or "").strip(),
        "sender": (os.getenv("PROVEN_SMTP_FROM") or "").strip(),
        "use_tls": (os.getenv("PROVEN_SMTP_TLS") or "1").strip().lower()
        not in ("0", "false", "no", "off"),
    }


def smtp_configured() -> bool:
    s = smtp_settings()
    return bool(s["host"] and s["sender"])


def notify_override() -> str:
    """A single address to send every notification to, overriding the account's own.

    Needed because the demo accounts use a `.demo` domain that can never receive mail. With
    no override configured, mail is composed and shown in the outbox rather than sent to an
    address that would bounce.
    """
    return (os.getenv("PROVEN_NOTIFY_TO") or "").strip()


def ai_enabled() -> bool:
    """Whether a configured shared profile may make a model call."""
    return llm.enabled()



def ai_status() -> dict:
    """Safe provider metadata for UI status; secrets are never included."""
    current = llm.settings()
    shared = llm.status()
    active = current.enabled
    return {
        "enabled": active,
        "mode": "LLM_ASSISTED" if active else "DETERMINISTIC_ONLY",
        "profile": current.profile.name,
        "chat_model": current.model if active else None,
        "embedding_model": None,
        "retrieval": "LEXICAL_BM25",
        "provider_guardrails": shared["guardrails"],
        "token_budgets": shared["token_budgets"],
        "notice": (
            "AI proposes, explains and routes within explicit consent. It never decides "
            "coverage, resolves precedence or computes a settlement figure; every "
            "user-visible model statement passes redaction and grounding gates."
            if active else
            "No usable model profile is configured, so the AI layer is off. Every finding, "
            "score and figure is produced by the deterministic engine and retrieval falls "
            "back to lexical scoring with page-anchored evidence."
        ),
        "key_source": "environment" if active and current.api_key else None,
    }
