"""Remembers which free model wins best-of runs, so the likely winner gets started first.

Stored in constructs/.model_stats.json:
    {"openai/gpt-oss-120b": {"runs": 5, "finishes": 4, "wins": 3, "score_sum": 390, "seconds_sum": 160}, ...}
"""

import json
import threading

from . import store

STATS_PATH = store.CONSTRUCTS / ".model_stats.json"
_lock = threading.Lock()


def load() -> dict[str, dict]:
    try:
        return json.loads(STATS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def record(entries: dict[str, tuple[int | None, float | None]], winner: str | None) -> None:
    """entries: model -> (score or None if it failed / didn't finish in time, seconds or None)."""
    with _lock:
        data = load()
        for model, (score, seconds) in entries.items():
            s = data.setdefault(model, {"runs": 0, "finishes": 0, "wins": 0, "score_sum": 0, "seconds_sum": 0.0})
            s["runs"] += 1
            if score is not None:
                s["finishes"] += 1
                s["score_sum"] += score
                s["seconds_sum"] += seconds or 0
            if model == winner:
                s["wins"] += 1
        STATS_PATH.parent.mkdir(exist_ok=True)
        STATS_PATH.write_text(json.dumps(data, indent=2))


def ranked(models: list[str]) -> list[str]:
    """Most likely winner first: win rate, then average speed. Unseen models keep their order."""
    data = load()

    def key(item: tuple[int, str]):
        i, m = item
        s = data.get(m)
        if not s or not s["runs"]:
            return (0, 0.0, 0.0, i)
        win_rate = s["wins"] / s["runs"]
        avg_seconds = s["seconds_sum"] / s["finishes"] if s["finishes"] else 999.0
        return (1, -win_rate, avg_seconds, i)

    return [m for _, m in sorted(enumerate(models), key=key)]


def table() -> list[dict]:
    rows = []
    for model, s in load().items():
        rows.append({
            "model": model,
            "runs": s["runs"],
            "wins": s["wins"],
            "finish_rate": s["finishes"] / s["runs"] if s["runs"] else 0,
            "avg_score": s["score_sum"] / s["finishes"] if s["finishes"] else None,
            "avg_seconds": s["seconds_sum"] / s["finishes"] if s["finishes"] else None,
        })
    return sorted(rows, key=lambda r: (-r["wins"], -(r["avg_score"] or 0)))
