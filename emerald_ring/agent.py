"""The forging agent: a hand-written agent loop, no frameworks.

    spec = forge("a giant mech fist")

The model designs the construct and calls our tools:
  web_search           (server-side, run by Anthropic) research real proportions
  present_plan         (only if the caller provides `review`) show the plan, get approval/tweaks
  write_scene_spec     store a draft spec
  validate_scene_spec  check it; on success it's saved to constructs/
  launch_viewer        (only if the caller provides it) open the browser

The loop runs until a spec validates; providers.py handles the API calls (Claude by default,
or open models on a free cloud tier: Groq or Cerebras). `forge()` has
no terminal or browser code, so a web backend can call it directly later.
"""

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config, free, stats, store
from .brief import BRIEF_MODELS
from .prompts import system_prompt
from .providers import (ForgeError, MalformedToolInput, ModelUnavailable, ToolCall, ToolResult, Usage,
                        make_provider)
from .inspect import Report, inspect
from .research import lookup
from . import parts as parts_lib
from . import reuse
from .validate import validate_spec

__all__ = ["forge", "forge_best", "ForgeError", "ForgeCancelled"]

# Free Groq models used for best-of-N. Each has its own per-minute limit, so they run in parallel.
BEST_OF_MODELS = {
    "groq": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"],
}

MAX_TURNS = 24  # hard stop for the loop (plan revisions use turns too)
MAX_NUDGES = 2  # times we remind the model to finish if it stops without a valid spec
MAX_JSON_RETRIES = 2

PLAN_TOOL = {
    "name": "present_plan",
    "description": (
        "Show the user your construct plan before building it. Returns APPROVED, or the user's "
        "requested changes (revise and call again). Never write the spec before approval."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Name of the construct."},
            "summary": {"type": "string", "description": "1-3 sentences: what it is, its pose, its overall look."},
            "research": {"type": "string", "description": "Key real-world facts/proportions used (empty if none)."},
            "dimensions": {"type": "string", "description": "Overall size, e.g. '~6 m long, 3.5 m tall'."},
            "components": {
                "type": "array",
                "description": "Main sub-assemblies, in build order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "shapes": {"type": "string", "description": "Primitives used, e.g. '4 cylinders + torus'."},
                        "notes": {"type": "string"},
                    },
                    "required": ["name", "shapes"],
                },
            },
            "animations": {"type": "array", "items": {"type": "string"}, "description": "Idle animations, one per line."},
            "estimated_parts": {"type": "integer"},
        },
        "required": ["title", "summary", "components", "animations", "estimated_parts"],
    },
}

WRITE_TOOL = {
    "name": "write_scene_spec",
    "description": (
        "Write the complete scene spec for the construct (replaces any previous draft). "
        "Always pass the whole spec, never a partial patch. Call validate_scene_spec afterwards."
    ),
    "stream": True,  # the spec is large; stream it as it's generated
    "input_schema": {
        "type": "object",
        "properties": {
            "spec": {
                "type": "object",
                "description": "The full scene spec, following the schema in the system prompt.",
            }
        },
        "required": ["spec"],
    },
}

VALIDATE_TOOL = {
    "name": "validate_scene_spec",
    "description": (
        "Validate the most recently written spec against the schema and the semantic rules "
        "(unique ids, known parents, no cycles, sorted keyframes...). Returns VALID, or a list of "
        "errors to fix. A valid spec is saved automatically."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

LOOKUP_TOOL = {
    "name": "lookup_reference",
    "description": (
        "Look up a real-world thing on Wikipedia and get its key facts and measurements "
        "(heights, lengths, spans, weights). Pass just the thing's name, e.g. 'Saturn V'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"subject": {"type": "string", "description": "The name of the thing, e.g. 'Saturn V'."}},
        "required": ["subject"],
    },
}

FIND_PARTS_TOOL = {
    "name": "find_parts",
    "description": (
        "Search the parts library: detailed components from earlier constructs (wheels, towers, "
        "heads, wings, hands, engines...). Use your own words, including related terms (for a "
        "wyvern, search 'dragon wing'). Returns component ids you can reuse with a 'use' entry."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "e.g. 'dragon head', 'wheel', 'castle tower'"}},
        "required": ["query"],
    },
}

LAUNCH_TOOL = {
    "name": "launch_viewer",
    "description": "Open the 3D viewer in the user's browser to forge the saved construct. Call once the spec is valid.",
    "input_schema": {"type": "object", "properties": {}},
}


class ForgeCancelled(Exception):
    """The user cancelled during plan review."""


@dataclass
class _State:
    description: str
    save: bool = True
    inspect_rounds: int = 0  # feedback rounds allowed after a spec is valid but looks wrong
    accept_score: int = 100  # a valid design scoring at least this is accepted without more rounds
    draft: dict | None = None
    best: dict | None = None  # best valid draft so far, and its inspection
    best_report: Report | None = None
    rounds_used: int = 0
    checked: dict | None = None  # the draft last checked, and the answer we gave
    check_result: ToolResult | None = None
    valid_spec: dict | None = None  # the final result (set when we finish)
    report: Report | None = None
    saved_path: Path | None = None
    launched: bool = False
    plan_approved: bool = False
    roomy: bool = False  # provider has room for longer reference lookups
    invalid: int = 0  # drafts that failed validation
    max_invalid: int | None = None  # give up on this model after this many (free chain only)
    model_name: str = ""
    usage: Usage = field(default_factory=Usage)

    def finish(self, emit) -> None:
        """Keep the best valid draft as the result, saving it unless told not to."""
        spec, self.report = self.best, self.best_report
        if self.save:
            self.saved_path = store.save(spec, self.description)
            self.valid_spec = store.load(self.saved_path)
            emit("saved", path=self.saved_path)
        else:
            self.valid_spec = {**spec, "prompt": self.description}

    def add_usage(self, u: Usage) -> None:
        t = self.usage
        t.input += u.input
        t.output += u.output
        t.cache_write += u.cache_write
        t.cache_read += u.cache_read
        t.turns += u.turns


Emit = Callable[..., None]
# review(plan) -> None/"" to approve, or the user's requested changes as text.
# It may raise ForgeCancelled to stop.
Review = Callable[[dict], str | None]


def forge(
    description: str,
    *,
    on_event: Emit | None = None,
    review: Review | None = None,
    launch_viewer: Callable[[Path], str] | None = None,
    research: bool = True,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    inspect_rounds: int | None = None,
    save: bool = True,
    brief: str | None = None,
    reuse_parts: bool = True,
    cancel: threading.Event | None = None,
) -> dict:
    """Design a construct from a description and return its (saved, valid) scene spec.

    on_event(kind, **data) receives progress events: "text", "tool_start", "tool_progress",
    "tool_done", "search", "saved", "launched", "retry", "notice", "done".
    review(plan), if given, lets the user approve or tweak the plan before anything is built.
    launch_viewer(path) -> url, if given, is offered to the model as the launch_viewer tool.
    research=False turns off web search.
    provider (anthropic | groq | cerebras) / model / effort override the RING_PROVIDER /
    RING_MODEL / RING_EFFORT settings.
    inspect_rounds: after a spec validates, how many times the geometry inspector may send it
    back to fix problems like floating parts (default: 2 for free models, 0 for Claude).
    save=False returns the spec without writing it to constructs/ (used by best-of-N).
    brief: a detailed design brief (see brief.py) the model should follow.
    """
    emit: Emit = on_event or (lambda kind, **data: None)
    chosen = provider or config.provider()
    if chosen == "free" or (chosen in free.FREE_CLOUD and not model):
        # The free chain (or just one service's part of it, e.g. --provider gemini).
        return forge_free(description, on_event=on_event, only=None if chosen == "free" else chosen,
                          review=review, launch_viewer=launch_viewer, research=research, effort=effort,
                          inspect_rounds=inspect_rounds, save=save, brief=brief, reuse_parts=reuse_parts)
    effort = effort or config.effort()
    llm = make_provider(provider, model, effort, research)
    # Claude searches the web itself; other models get the free Wikipedia lookup tool instead.
    use_lookup = research and not llm.supports_research
    tools = (([LOOKUP_TOOL] if use_lookup else []) + ([FIND_PARTS_TOOL] if reuse_parts else []) + ([PLAN_TOOL] if review else [])
             + [WRITE_TOOL, VALIDATE_TOOL] + ([LAUNCH_TOOL] if launch_viewer else []))
    state = _State(description, save=save,
                   inspect_rounds=llm.inspect_rounds if inspect_rounds is None else inspect_rounds,
                   roomy=not llm.compact_prompt,
                   max_invalid=4 if llm.name != "anthropic" else None,
                   # Free tiers are slow to iterate: take a good design rather than polish it.
                   accept_score=100 if llm.name == "anthropic" else 85,
                   model_name=llm.model)
    llm.start(system_prompt("lookup" if use_lookup else (research and llm.supports_research),
                            compact=llm.compact_prompt, reuse=reuse_parts),
              f"Forge this construct: {description}"
              + (f"\n\nFollow this design brief closely:\n{brief}" if brief else ""))
    nudges = 0
    json_retries = 0

    for _ in range(MAX_TURNS):
        if cancel is not None and cancel.is_set():
            raise ForgeCancelled()  # another model already won the race
        try:
            turn = llm.turn(tools, emit)
        except ForgeError:
            if state.best is None:
                raise
            emit("notice", text="stopping early; keeping the best valid version so far")
            state.finish(emit)
            break
        except MalformedToolInput:
            # Tool-call JSON broke before the call completed, so there's no call id to answer.
            # Re-issue the turn (bounded).
            json_retries += 1
            if json_retries > MAX_JSON_RETRIES:
                raise ForgeError("The model kept producing malformed JSON for the spec.")
            emit("retry", reason="malformed tool call, retrying")
            llm.add_user(
                "Your last tool call could not be parsed, so it was discarded. Call the tool again "
                "with arguments that are exactly one valid JSON object (no comments, no trailing text)."
            )
            continue
        json_retries = 0
        state.add_usage(turn.usage)

        if turn.stop == "refusal":
            raise ForgeError("The request was declined. Try describing the construct differently.")
        if turn.stop == "pause":
            continue  # a server-side tool (web search) paused; re-send to resume

        if not turn.tool_calls:
            if state.best is not None:  # stopped after inspection feedback: keep the best
                state.finish(emit)
                break
            if nudges >= MAX_NUDGES:
                raise ForgeError("The model stopped without producing a valid scene spec.")
            nudges += 1
            emit("retry", reason="no valid spec yet, nudging")
            llm.add_user(
                "No valid scene spec exists yet. Call write_scene_spec with the complete spec, "
                "then validate_scene_spec."
            )
            continue

        if turn.stop == "max_tokens":
            # A truncated tool input can still parse as a (partial) object, so never run it.
            results = [ToolResult(c.id, (
                "Your output was cut off before this tool call finished (max_tokens). Write a more "
                "compact spec in a single call: fewer parts, fewer tube points, no extra whitespace."
            ), is_error=True) for c in turn.tool_calls]
        else:
            results = [_run_tool(c, state, emit, review, launch_viewer) for c in turn.tool_calls]
        llm.add_tool_results(results)

        if state.valid_spec is not None:
            # Done as soon as a spec validates: skip the extra turn(s) the model would spend
            # opening the viewer and summarising. Open the viewer ourselves instead.
            if launch_viewer and not state.launched:
                emit("launched", url=launch_viewer(state.saved_path))
                state.launched = True
            break
    else:
        if state.best is None:
            raise ForgeError(f"Gave up after {MAX_TURNS} turns without a valid spec.")
        state.finish(emit)

    emit("done", usage=state.usage, provider=llm.name, model=llm.model, effort=effort,
         cost=llm.cost(state.usage), path=state.saved_path, launched=state.launched,
         score=state.report.score if state.report else None)
    return state.valid_spec


def _run_tool(call: ToolCall, state: _State, emit: Emit, review, launch_viewer) -> ToolResult:
    ok = lambda text: ToolResult(call.id, text)  # noqa: E731
    err = lambda text: ToolResult(call.id, text, is_error=True)  # noqa: E731

    if call.input is None and call.name in ("present_plan", "write_scene_spec"):
        emit("tool_done", name=call.name, ok=False, summary="unreadable JSON")
        return err(json.dumps({"INVALID_JSON": call.raw[:2000]}) + " Call the tool again with valid JSON.")

    if call.name == "lookup_reference":
        subject = str((call.input or {}).get("subject") or (call.input or {}).get("query") or "").strip()
        if not subject:
            return err("Pass the name of the thing to look up, e.g. {\"subject\": \"Saturn V\"}.")
        emit("search", query=subject)
        result = lookup(subject, max_chars=1800 if state.roomy else 1200)
        emit("tool_done", name=call.name, ok=not result.startswith(("No Wikipedia", "Reference lookup unavailable")),
             summary=result.split("\n", 1)[0][:80])
        return ok(result)

    if call.name == "find_parts":
        query = str((call.input or {}).get("query") or "").strip()
        found = parts_lib.search(query, k=5) if query else []
        emit("parts_found", query=query, count=len(found))
        if not found:
            return ok(f"No parts match {query!r}. Try a simpler or related word, or design it yourself.")
        return ok("Reusable components (use the id exactly in a 'use' entry):\n"
                  + "\n".join(reuse.describe(c) for c in found))

    if call.name == "present_plan" and review:
        plan = call.input
        if not isinstance(plan.get("components"), list) or not plan.get("title"):
            emit("tool_done", name=call.name, ok=False, summary="malformed plan")
            return err("The plan needs at least a title and a components list. Call present_plan again.")
        emit("tool_done", name=call.name, ok=True, summary=f"plan ready ({plan.get('estimated_parts', '?')} parts)")
        feedback = (review(plan) or "").strip()
        if not feedback:
            state.plan_approved = True
            emit("plan_approved")
            return ok("APPROVED. Now write the full scene spec following this plan.")
        emit("plan_feedback", feedback=feedback)
        return ok(f"CHANGES REQUESTED by the user: {feedback}\nRevise the plan accordingly and call present_plan again.")

    if call.name == "write_scene_spec":
        if review and not state.plan_approved:
            emit("tool_done", name=call.name, ok=False, summary="plan not approved yet")
            return err("The plan hasn't been approved yet. Call present_plan first.")
        spec = call.input.get("spec")
        if isinstance(spec, str):  # tolerate a JSON-encoded string
            try:
                spec = json.loads(spec)
            except json.JSONDecodeError:
                spec = None
        if not isinstance(spec, dict):
            emit("tool_done", name=call.name, ok=False, summary="unreadable spec")
            return err(json.dumps({"INVALID_JSON": call.raw[:2000]}) + " Pass the spec as a JSON object.")
        spec, used, reuse_errors = reuse.expand(spec)
        if reuse_errors:
            emit("tool_done", name=call.name, ok=False, summary=f"{len(reuse_errors)} reuse error(s)",
                 errors=reuse_errors)
            state.invalid += 1
            return err("Some 'use' entries couldn't be expanded:\n" + "\n".join(f"- {e}" for e in reuse_errors))
        if used:
            emit("reused", used=used, saved_tokens=sum(u["chars"] for u in used) // 3)
        state.draft = spec
        n_parts, n_anims = len(spec.get("parts", [])), len(spec.get("animations", []))
        emit("tool_done", name=call.name, ok=True, summary=f"{n_parts} parts, {n_anims} animations")
        # Check it straight away: saves the model a whole round trip per attempt.
        result = _check(call, state, emit)
        result.content = f"Draft written ({n_parts} parts, {n_anims} animations), then checked: " + result.content
        return result

    if call.name == "validate_scene_spec":
        if state.draft is None:
            emit("tool_done", name=call.name, ok=False, summary="nothing written yet")
            return err("Nothing to validate. Call write_scene_spec first.")
        if state.checked is state.draft and state.check_result is not None:
            # Already checked when it was written; repeat the answer without re-running it.
            return ToolResult(call.id, state.check_result.content, state.check_result.is_error)
        return _check(call, state, emit)

    if call.name == "launch_viewer" and launch_viewer:
        if state.saved_path is None:
            emit("tool_done", name=call.name, ok=False, summary="no valid spec yet")
            return err("There is no valid, saved spec to show yet.")
        url = launch_viewer(state.saved_path)
        state.launched = True
        emit("tool_done", name=call.name, ok=True, summary="browser opened")
        emit("launched", url=url)
        return ok(f"Viewer opened at {url}; the ring is forging the construct now.")

    emit("tool_done", name=call.name, ok=False, summary="unknown tool")
    return err(f"Unknown tool: {call.name}")


def _check(call: ToolCall, state: _State, emit: Emit) -> ToolResult:
    """Validate the current draft, then inspect its geometry; keep the best valid version."""
    result = _check_draft(call, state, emit)
    state.checked, state.check_result = state.draft, ToolResult(call.id, result.content, result.is_error)
    return result


def _check_draft(call: ToolCall, state: _State, emit: Emit) -> ToolResult:
    ok = lambda text: ToolResult(call.id, text)  # noqa: E731
    err = lambda text: ToolResult(call.id, text, is_error=True)  # noqa: E731
    name = "validate_scene_spec"
    errors = validate_spec(state.draft)
    title = str(state.draft.get("title", "")).lower()
    if "sentry walker" in title and "walker" not in state.description.lower():
        errors = [f"This is the reference example, not what the user asked for ({state.description!r}). "
                  "Design that instead."] + errors
    if errors:
        emit("tool_done", name=name, ok=False, summary=f"{len(errors)} error(s)", errors=errors)
        state.invalid += 1
        if state.max_invalid and state.invalid >= state.max_invalid and state.best is None:
            # This model is struggling; let the free chain hand the job to the next one.
            raise ModelUnavailable(f"{state.model_name} couldn't produce a valid design in "
                                   f"{state.invalid} tries", state.model_name, 60)
        shown = "\n".join(f"- {e}" for e in errors[:40])
        more = f"\n... and {len(errors) - 40} more" if len(errors) > 40 else ""
        return err(f"INVALID. Fix these, then write_scene_spec the full corrected spec:\n{shown}{more}")
    report = inspect(state.draft)
    if state.best_report is None or report.score > state.best_report.score:
        state.best, state.best_report = state.draft, report
    emit("tool_done", name=name, ok=True, summary=f"valid · inspector score {report.score}/100")
    if report.issues and report.score < state.accept_score and state.rounds_used < state.inspect_rounds:
        state.rounds_used += 1
        emit("inspect", score=report.score, issues=report.issues, round=state.rounds_used)
        problems = "\n".join(f"- {i}" for i in report.issues)
        return ok(
            f"VALID, but the geometry inspector found problems (score {report.score}/100):\n"
            f"{problems}\nFix ALL of them, then write_scene_spec the full corrected spec and "
            "validate it again."
        )
    state.finish(emit)
    return ok("VALID. Saved." if state.save else "VALID.")


def forge_free(description: str, *, on_event: Emit | None = None, only: str | None = None,
               race: bool = False, **kwargs) -> dict:
    """Forge with free models, walking down the free chain (free.py) until one works.

    A model that's used up or overloaded is remembered and skipped, and the next one takes over.
    If a model gets a valid design before running out, forge() keeps that design.
    """
    emit: Emit = on_event or (lambda kind, **data: None)
    chain = [pm for pm in free.available() if only in (None, pm[0])]
    if only and not free.has_key(only):
        raise ForgeError(f"{only} isn't set up. Get a free key at {free.FREE_CLOUD[only]['signup']} and "
                         f"add {free.FREE_CLOUD[only]['key_env']}=... to .env")
    if not free.configured():
        raise ForgeError(
            "Free mode needs a free key in .env:\n"
            "  GEMINI_API_KEY=...  (https://aistudio.google.com/apikey, no credit card)\n"
            "  GROQ_API_KEY=...    (https://console.groq.com/keys, no credit card)")
    if not chain:
        wait = free.next_reset()
        raise ForgeError("Every free model you have a key for is used up for now"
                         + (f"; the next one frees up in ~{wait / 60:.0f} min" if wait else "")
                         + ". Add another free key (Gemini or Groq) to .env, or use Claude.")
    # Health check: ping every candidate at once and skip the busy / used-up ones right away.
    chain = free.probe(chain, emit=emit) or chain
    if race and kwargs.get("review") is None:
        racers = free.pick_racers(chain, 2)
        if len(racers) >= 2:
            return forge_best(description, pairs=racers, on_event=on_event,
                              launch_viewer=kwargs.get("launch_viewer"), brief=kwargs.get("brief"),
                              research=kwargs.get("research", True), head_start=0, good_enough=85,
                              deadline=60)
    skipped = []
    for provider_name, model_id in chain:
        if skipped:
            emit("notice", text=f"trying {model_id} ({provider_name})")
        try:
            return forge(description, on_event=on_event, provider=provider_name, model=model_id, **kwargs)
        except ModelUnavailable as e:
            free.mark_unavailable(e.model, e.retry_in)
            skipped.append(f"{model_id}: {e}")
            emit("notice", text=f"{e}; switching to the next free model")
    raise ForgeError("No free model could finish this right now:\n  " + "\n  ".join(skipped)
                     + "\nTry again later, add another free key, or use Claude.")


def forge_best(
    description: str,
    *,
    provider: str | None = None,
    models: list[str] | None = None,
    on_event: Emit | None = None,
    launch_viewer: Callable[[Path], str] | None = None,
    deadline: float = 75.0,
    good_enough: int = 95,
    head_start: float = 5.0,
    give_up_after: float = 180.0,
    brief: str | None = None,
    research: bool = True,
    pairs: list[tuple[str, str]] | None = None,
) -> dict:
    """Run several free models on the same idea and keep the best-scoring design.

    Every candidate goes through the normal forge() loop (validator + inspector feedback) but
    isn't saved. The model that has won most often (stats.py) starts first, with a short head
    start; the others follow as a safety net. We stop as soon as any design scores at least
    `good_enough`; otherwise, after `deadline` seconds, we take the best finished design (or wait
    for the first one, up to `give_up_after` seconds). The winner is saved and returned, and the
    result is recorded in stats.
    """
    emit: Emit = on_event or (lambda kind, **data: None)
    provider = provider or config.provider()
    if pairs:
        pass  # explicit (provider, model) racers, e.g. from forge_free's health check
    elif provider in ("free", "anthropic"):
        # Pick up to 3 available free models, spread across services (each has its own limits).
        chain = free.available()
        picked = []
        for p_name in dict.fromkeys(p for p, _ in chain):
            picked += [pm for pm in chain if pm[0] == p_name][:1]
        picked += [pm for pm in chain if pm not in picked]
        pairs = picked[:3]
        if not pairs:
            raise ForgeError("No free model is available for --best right now. Add a free key "
                             "(GEMINI_API_KEY or GROQ_API_KEY) to .env, or try again later.")
    else:
        pairs = [(provider, m) for m in (models or BEST_OF_MODELS.get(provider) or [])]
        if not pairs:
            raise ForgeError(f"Best-of mode needs a list of models for {provider}.")
    provider_of = {m: p for p, m in pairs}
    order = stats.ranked([m for _, m in pairs])
    if brief:
        # The brief was written by gpt-oss-120b, which used up part of its per-minute budget:
        # start it last so it doesn't begin with a cooldown.
        order = [m for m in order if m != BRIEF_MODELS.get("groq")] + \
                [m for m in order if m == BRIEF_MODELS.get("groq")]

    results: dict[str, tuple[dict, Report] | Exception] = {}
    seconds: dict[str, float] = {}
    done = threading.Condition()
    stop = threading.Event()  # tells the losing candidates to stop (and stop using free quota)
    start = time.monotonic()

    def run(model: str, delay: float) -> None:
        if delay:
            time.sleep(delay)

        def tagged(kind, **data):
            emit(kind, candidate=model, **data)
        try:
            spec = forge(description, on_event=tagged, provider=provider_of[model], model=model,
                         research=research and not brief, save=False, brief=brief, cancel=stop)
            outcome: tuple[dict, Report] | Exception = (spec, inspect(spec))
        except Exception as e:  # one candidate failing must not stop the others
            if isinstance(e, ModelUnavailable):
                free.mark_unavailable(e.model, e.retry_in)
            outcome = e
        with done:
            results[model] = outcome
            seconds[model] = time.monotonic() - start
            emit("candidate_done", candidate=model, ok=not isinstance(outcome, Exception),
                 score=None if isinstance(outcome, Exception) else outcome[1].score,
                 error=str(outcome) if isinstance(outcome, Exception) else None)
            done.notify_all()

    emit("start_order", models=order, head_start=head_start)
    for i, m in enumerate(order):
        threading.Thread(target=run, args=(m, head_start if i else 0.0), daemon=True).start()

    def successes():
        return {m: r for m, r in results.items() if not isinstance(r, Exception)}

    early = False
    with done:
        while len(results) < len(order):
            if any(r[1].score >= good_enough for r in successes().values()):
                early = len(results) < len(order)
                break  # a design is good enough: don't wait for the others
            elapsed = time.monotonic() - start
            if elapsed >= deadline and successes():
                break  # deadline passed and we have at least one design
            if elapsed >= give_up_after:
                break  # nobody finished in time (free-tier limits are probably exhausted)
            done.wait(timeout=max(deadline - elapsed, 2.0))
        snapshot = dict(results)
        snap_seconds = dict(seconds)
        stop.set()

    winners = {m: r for m, r in snapshot.items() if not isinstance(r, Exception)}
    if not winners:
        stats.record({m: (None, None) for m in order}, None)
        reasons = "; ".join(f"{m.split('/')[-1]}: {r}" for m, r in snapshot.items())
        unfinished = [m.split("/")[-1] for m in order if m not in snapshot]
        if unfinished:
            reasons += f"; still working after {round(give_up_after)} s: {', '.join(unfinished)}"
        raise ForgeError(f"No model produced a valid design. {reasons}. If free limits are used up, "
                         "try again later or use Claude (drop --best).")
    best_model, (spec, report) = max(winners.items(), key=lambda kv: kv[1][1].score)
    # Candidates still running when we stopped count as "didn't finish" this time.
    stats.record({m: ((snapshot[m][1].score, snap_seconds[m]) if m in winners else (None, None))
                  for m in order}, best_model)

    path = store.save({k: v for k, v in spec.items() if k != "prompt"}, description)
    emit("best_chosen", model=best_model, score=report.score, finished=len(winners),
         total=len(order), seconds=round(time.monotonic() - start), early=early)
    emit("saved", path=path)
    if launch_viewer:
        emit("launched", url=launch_viewer(path))
    return store.load(path)
