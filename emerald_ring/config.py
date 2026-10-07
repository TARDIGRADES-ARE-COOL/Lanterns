"""Settings, loaded from the environment and the project's .env file."""

import os

from . import ROOT

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "low"


def load_env() -> None:
    """Read KEY=value lines from .env into os.environ (real env vars take priority)."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value and key not in os.environ:
            os.environ[key] = value


def api_key() -> str | None:
    load_env()
    return os.environ.get("ANTHROPIC_API_KEY") or None


def provider() -> str:
    """Which provider to use when none is given.

    RING_PROVIDER wins if set. Otherwise: Claude if there's an Anthropic key, else the free
    chain if there's any free key (Gemini/Groq/Cerebras), else "anthropic" (whose error message
    then explains how to get a key, free or paid).
    """
    load_env()
    chosen = os.environ.get("RING_PROVIDER")
    if chosen:
        return chosen
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if any(os.environ.get(k) for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY")):
        return "free"
    return "anthropic"


def model() -> str:
    """The Claude model. RING_MODEL counts only if it names a Claude model."""
    load_env()
    m = os.environ.get("RING_MODEL", "")
    return m if m.startswith("claude") else DEFAULT_MODEL


def provider_model() -> str | None:
    """The model for a free-cloud provider, from RING_MODEL (None if unset or a Claude id)."""
    load_env()
    m = os.environ.get("RING_MODEL", "")
    return m if m and not m.startswith("claude") else None


def effort() -> str:
    """How hard Claude thinks: low | medium | high | xhigh | max."""
    load_env()
    return os.environ.get("RING_EFFORT", DEFAULT_EFFORT)
