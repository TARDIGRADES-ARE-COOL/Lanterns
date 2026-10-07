"""Design brief: a reasoning model expands a short idea into a detailed, specific brief.

    brief = write_brief("a giant mech fist")

"a giant mech fist" leaves a lot unsaid, and free models fill the gaps with something vague
(an open hand, no forearm). A top free reasoning model thinks the design through first and
writes down exactly what to build: pose, components with counts, proportions in metres, how
pieces connect, distinctive details and animations. The builders then work from the brief.
"""

import os
import time

import openai

from . import config
from .providers import FREE_CLOUD, ForgeError, _retry_after

BRIEF_MODELS = {"groq": "openai/gpt-oss-120b"}

BRIEF_PROMPT = """\
You are the design director for the Emerald Ring, which forges glowing green hard-light
constructs out of simple 3D shapes (boxes, spheres, cylinders, cones, tori, tubes, extruded
flat outlines). A builder will turn your brief into 3D, so it must be concrete and spatial.

Think the design through carefully, then write a COMPACT brief of at most 150 words (it must
fit in a small token budget), using terse bullet points under these headings:

POSE: what rests on the ground (y = 0, Y is up), which way it faces (+Z is the front), its
overall height, width and depth in metres (keep it within 3-10 m), and its stance or angle.
COMPONENTS: every piece needed for it to be instantly recognisable: count, size in metres,
where it attaches, which way it points or curls. Mirror left/right pieces explicitly.
DETAILS: 3-5 distinctive features and where they go.
ANIMATIONS: 2-4 subtle idle motions (what moves, around which joint).

Be specific with numbers and directions. No JSON, no code, no preamble.
"""


def write_brief(description: str, provider: str = "groq", on_event=None) -> str:
    emit = on_event or (lambda kind, **data: None)
    model = BRIEF_MODELS.get(provider)
    cfg = FREE_CLOUD.get(provider)
    if not model or not cfg:
        raise ForgeError(f"Design briefs aren't configured for {provider}.")
    config.load_env()
    key = os.environ.get(cfg["key_env"])
    if not key:
        raise ForgeError(f"{provider} isn't set up. Get a free key at {cfg['signup']} and add "
                         f"{cfg['key_env']}=... to .env")
    client = openai.OpenAI(api_key=key, base_url=cfg["base_url"], timeout=180, max_retries=1)
    # "high" reasoning can burn the whole answer allowance on thinking and return nothing, so
    # start at "medium" (plenty for a 150-word brief) and fall back to "low".
    efforts = ["medium", "low", "low"]
    for attempt, effort in enumerate(efforts):
        try:
            r = client.chat.completions.create(
                model=model,
                reasoning_effort=effort,
                max_completion_tokens=7000,  # prompt + answer must fit Groq's 8,000/min cap
                messages=[{"role": "system", "content": BRIEF_PROMPT},
                          {"role": "user", "content": f"Construct: {description}"}],
            )
            choice = r.choices[0]
            text = (choice.message.content or "").strip()
            if len(text) > 100:
                return text
            why = "ran out of room while thinking" if choice.finish_reason == "length" else "came back empty"
            emit("retry", reason=f"brief {why}, retrying with less reasoning")
        except openai.RateLimitError as e:
            wait = _retry_after(e) or 60
            if wait > 90:
                raise ForgeError("free-tier daily limit reached for the brief model")
            emit("cooldown", seconds=round(wait))
            time.sleep(wait + 1)
        except openai.APIConnectionError:
            raise ForgeError("couldn't reach the free model host")
        except openai.APIStatusError as e:
            raise ForgeError(f"brief model error {e.status_code}")
    raise ForgeError("the brief model didn't return a brief")
