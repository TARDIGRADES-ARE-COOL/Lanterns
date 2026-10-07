"""Free research for any model: look things up on Wikipedia (no key, no sign-up).

Claude has Anthropic's built-in web search. Gemini's Google Search grounding needs billing, and
Groq has no search at all, so free models get this instead: a `lookup_reference` tool that
finds the best-matching Wikipedia article and returns its opening plus the sentences that
contain measurements (metres, feet, tonnes...), which is what designing a construct needs.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "EmeraldRing/0.1 (personal hobby project; hard-light construct designer)"}

_MEASURE = re.compile(
    r"\d[\d,.]*\s?(?:-\s?)?(?:m|metres?|meters?|ft|feet|foot|km|cm|mm|inches|tonnes?|tons?|kg|"
    r"lb|pounds|mph|km/h)\b(?!\s+[A-Z][a-z])", re.IGNORECASE)


def _get(params: dict) -> dict:
    url = API + "?" + urllib.parse.urlencode({**params, "format": "json"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:  # Wikipedia asks us to slow down
                time.sleep(3)
                continue
            raise


def _find_title(subject: str) -> tuple[str | None, list[str]]:
    """Best article title for a subject: title matches first, then full-text search."""
    titles = _get({"action": "opensearch", "search": subject, "limit": 5, "namespace": 0})
    titles = titles[1] if isinstance(titles, list) and len(titles) > 1 else []
    if not titles:
        hits = _get({"action": "query", "list": "search", "srsearch": subject, "srlimit": 5})["query"]["search"]
        titles = [h["title"] for h in hits]
    if not titles:
        return None, []
    s = subject.lower()
    exact = [t for t in titles if t.lower() == s]
    named = [t for t in titles if t.lower() in s or s in t.lower()]
    best = exact[0] if exact else (min(named, key=len) if named else titles[0])
    return best, [t for t in titles if t != best]


def lookup(subject: str, max_chars: int = 1800) -> str:
    """Key facts about `subject` (the thing's name, e.g. "Saturn V") from Wikipedia, kept short
    enough for tight token budgets. Never raises: on any problem it says to use own knowledge."""
    try:
        return _lookup(subject, max_chars)
    except Exception as e:  # network trouble must never break a forge
        return f"Reference lookup unavailable right now ({type(e).__name__}). Use your own knowledge."


def _lookup(subject: str, max_chars: int) -> str:
    title, others = _find_title(subject)
    if not title:
        return f"No Wikipedia article found for {subject!r}. Use your own knowledge."

    pages = _get({"action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1,
                  "titles": title})["query"]["pages"]
    text = next(iter(pages.values())).get("extract", "") or ""
    intro = text.split("\n\n")[0][:500]
    body = re.sub(r"=+[^=\n]+=+", " ", text)  # drop section headings
    body = re.sub(r"\s+", " ", body)
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", body)
    measures, used = [], len(intro)
    for s in sentences:
        s = s.strip()
        if _MEASURE.search(s) and s not in intro and 20 < len(s) < 400:
            if used + len(s) > max_chars:
                break
            measures.append(s)
            used += len(s)

    others = ", ".join(others)[:200]
    url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
    out = [f"Wikipedia: {title} ({url})", intro]
    if measures:
        out.append("Measurements and dimensions:\n" + "\n".join(f"- {m}" for m in measures))
    if others:
        out.append(f"Other matching articles: {others}")
    return "\n".join(out)
