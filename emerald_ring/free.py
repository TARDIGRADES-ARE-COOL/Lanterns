"""The free model chain: which free models to try, in which order, and which are used up.

Free tiers run out (daily limits) or get busy ("high demand"). Instead of failing, free mode
walks down this chain, best designer first, and skips any model it already knows is used up.
Only providers you have a key for are included, so it works for anyone who adds just one free
key (Gemini and/or Groq) to .env.

Used-up models are remembered in constructs/.free_status.json until their limit resets.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import openai

from . import config, store
from .providers import FREE_CLOUD

# Best designer first. Each model has its own free allowance, so variety keeps things working.
CHAIN: list[tuple[str, str]] = [
    ("gemini", "gemini-3.5-flash"),
    ("gemini", "gemini-3.7-flash"),
    ("gemini", "gemini-3.8-flash"),
    ("groq", "openai/gpt-oss-120b"),
    ("gemini", "gemini-3.5-flash-lite"),
    ("groq", "qwen/qwen3.8-27b"),
    ("groq", "openai/gpt-oss-20b"),
]

STATUS_PATH = store.CONSTRUCTS / ".free_status.json"


def _load() -> dict[str, float]:
    try:
        return json.loads(STATUS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def mark_unavailable(model: str, retry_in: float) -> None:
    """Remember that `model` can't be used for about `retry_in` seconds."""
    status = _load()
    status[model] = time.time() + max(retry_in, 30)
    STATUS_PATH.parent.mkdir(exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, indent=2))


def has_key(provider: str) -> bool:
    config.load_env()
    return bool(os.environ.get(FREE_CLOUD[provider]["key_env"]))


def configured() -> list[tuple[str, str]]:
    """Chain entries you have a key for (whether or not they're currently used up)."""
    return [(p, m) for p, m in CHAIN if has_key(p)]


def available() -> list[tuple[str, str]]:
    """Chain entries you have a key for that aren't known to be used up right now."""
    now = time.time()
    status = _load()
    return [(p, m) for p, m in configured() if status.get(m, 0) <= now]


def next_reset() -> float | None:
    """Seconds until the soonest used-up model becomes available again (None if none)."""
    now = time.time()
    waits = [t - now for m, t in _load().items() if t > now and any(m == cm for _, cm in configured())]
    return min(waits) if waits else None


def _ping(provider: str, model: str) -> tuple[str, float]:
    """One tiny request: ("ok" | "busy" | "limited", seconds until worth retrying)."""
    from .providers import _retry_after  # local import: providers imports nothing from here

    cfg = FREE_CLOUD[provider]
    client = openai.OpenAI(api_key=os.environ.get(cfg["key_env"]), base_url=cfg["base_url"],
                           timeout=6, max_retries=0)
    try:
        client.chat.completions.create(model=model, max_completion_tokens=16,
                                       messages=[{"role": "user", "content": "ok"}])
        return "ok", 0
    except openai.RateLimitError as e:
        return "limited", _retry_after(e) or 60
    except (openai.APIStatusError, openai.APIConnectionError, openai.APITimeoutError) as e:
        code = getattr(e, "status_code", None)
        if code in (400, 404):  # model not offered on this key: don't try it for a day
            return "limited", 24 * 3600
        return "busy", 180


def probe(chain: list[tuple[str, str]], emit=None) -> list[tuple[str, str]]:
    """Ping every candidate at once (at most ~6 s) and return only the healthy ones, in chain order.

    Busy or used-up models are remembered (mark_unavailable) so later runs skip them too.
    """
    config.load_env()
    if not chain:
        return []
    with ThreadPoolExecutor(max_workers=len(chain)) as ex:
        results = list(ex.map(lambda pm: _ping(*pm), chain))
    healthy = []
    for (p, m), (state, retry_in) in zip(chain, results):
        if state == "ok":
            healthy.append((p, m))
        else:
            mark_unavailable(m, retry_in)
    if emit:
        emit("probe", healthy=[m for _, m in healthy],
             skipped=[(m, st) for (_, m), (st, _) in zip(chain, results) if st != "ok"])
    return healthy


def pick_racers(healthy: list[tuple[str, str]], n: int = 2) -> list[tuple[str, str]]:
    """The best healthy models, preferring different services (their limits are independent)."""
    picked = []
    for service in dict.fromkeys(p for p, _ in healthy):
        picked += [pm for pm in healthy if pm[0] == service][:1]
    picked.sort(key=lambda pm: healthy.index(pm))
    picked += [pm for pm in healthy if pm not in picked]
    return picked[:n]
