"""Model providers for the forge agent.

  anthropic  Claude (paid): best designs, web research, prompt caching
  gemini     Google's free tier: strong Flash models, roomy limits
  groq       free cloud tier, open models, very fast (~500-800 tokens/s)
  cerebras   open models, very fast (now credit-based rather than a free tier)

Each provider runs one model turn at a time and keeps its own message history in its API's
format, so the loop in agent.py works the same for every model:

    provider.start(system, first_user_message)
    turn = provider.turn(tools, emit)          # -> Turn(text, tool_calls, stop, usage)
    provider.add_tool_results([...])           # answer the tool calls
    provider.add_user("...")                   # or nudge with a plain message
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic
import openai

from . import config

Emit = Callable[..., None]


class ForgeError(Exception):
    """The agent couldn't produce a valid construct."""


class ModelUnavailable(ForgeError):
    """This model can't be used right now (daily limit, overloaded, rate-limited).

    Not a failure of the request itself: the free chain moves on to the next model.
    `retry_in` is roughly how many seconds until it's worth trying this model again.
    """

    def __init__(self, message: str, model: str, retry_in: float):
        super().__init__(message)
        self.model = model
        self.retry_in = retry_in


class MalformedToolInput(Exception):
    """The model produced tool-call JSON so broken the turn has to be re-issued."""


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict | None  # None when the arguments weren't valid JSON
    raw: str = ""  # the raw arguments, for error messages


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    turns: int = 0


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall]
    stop: str  # "end" | "tool_use" | "max_tokens" | "refusal" | "pause"
    usage: Usage = field(default_factory=Usage)


@dataclass
class ToolResult:
    id: str
    content: str
    is_error: bool = False


# Neutral tool definition: {"name", "description", "input_schema", "stream"?}.
# "stream": True marks tools with big inputs (the spec) that should stream as generated.


# ---------------------------------------------------------------- Anthropic

ANTHROPIC_PRICES = {  # per million tokens, for the cost estimate only
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.20},
}

# The basic search variant: results go straight to Claude. (The newer _20260209 variant filters
# results by running model-written code, which kept failing on its first try in testing.)
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}


class AnthropicProvider:
    name = "anthropic"
    supports_research = True
    compact_prompt = False
    inspect_rounds = 0  # Claude rarely needs it; keeps forging at ~20 s
    max_tokens = 64000  # specs can be long; streaming keeps this safe from HTTP timeouts

    def __init__(self, model: str, effort: str, research: bool):
        key = config.api_key()
        if not key:
            raise ForgeError(
                "No API key found. Add one to the .env file:\n"
                "  free:   GEMINI_API_KEY=...  (https://aistudio.google.com/apikey, no credit card)\n"
                "          GROQ_API_KEY=...    (https://console.groq.com/keys, no credit card)\n"
                "  Claude: ANTHROPIC_API_KEY=... (https://console.anthropic.com, paid, best quality)")
        self.client = anthropic.Anthropic(api_key=key)
        self.model, self.effort, self.research = model, effort, research
        self.system = ""
        self.messages: list[dict] = []

    def start(self, system: str, user_text: str) -> None:
        self.system = system
        self.messages = [{"role": "user", "content": user_text}]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r.id, "content": r.content, **({"is_error": True} if r.is_error else {})}
            for r in results
        ]})

    def _tools(self, tools: list[dict]) -> list[dict]:
        out = [WEB_SEARCH_TOOL] if self.research else []
        for t in tools:
            d = {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            if t.get("stream"):
                d["eager_input_streaming"] = True  # stream the big spec as it's generated
            out.append(d)
        return out

    def turn(self, tools: list[dict], emit: Emit) -> Turn:
        try:
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self.system,
                tools=self._tools(tools),
                messages=self.messages,
                cache_control={"type": "ephemeral"},  # cache system prompt + growing history
                output_config={"effort": self.effort},
                # If a safety classifier declines, retry on a fallback model inside the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                tool_name, chars = None, 0
                for event in stream:
                    if event.type == "text":
                        emit("text", text=event.text)
                    elif event.type == "content_block_start" and event.content_block.type in ("tool_use", "server_tool_use"):
                        tool_name, chars = event.content_block.name, 0
                        emit("tool_start", name=tool_name)
                    elif event.type == "input_json":
                        chars += len(event.partial_json)
                        emit("tool_progress", name=tool_name, chars=chars)
                response = stream.get_final_message()
        except ValueError as e:
            # Tool-input JSON the SDK couldn't parse at all, before the block completed.
            raise MalformedToolInput() from e

        for block in response.content:
            if block.type == "server_tool_use" and block.name == "web_search":
                emit("search", query=(block.input or {}).get("query", ""))
            elif block.type == "web_search_tool_result" and not isinstance(block.content, list):
                # Server-tool errors don't raise: success is a list, an error is an object.
                emit("search_error", code=getattr(block.content, "error_code", "unknown"))

        self.messages.append({"role": "assistant", "content": response.content})
        u = response.usage
        usage = Usage(
            input=u.input_tokens or 0,
            output=u.output_tokens or 0,
            cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            turns=1,
        )
        stop = {"tool_use": "tool_use", "max_tokens": "max_tokens", "refusal": "refusal",
                "pause_turn": "pause"}.get(response.stop_reason, "end")
        calls = [ToolCall(b.id, b.name, b.input if isinstance(b.input, dict) else None, json.dumps(b.input))
                 for b in response.content if b.type == "tool_use"]
        text = "".join(b.text for b in response.content if b.type == "text")
        return Turn(text, calls, stop, usage)

    def cost(self, usage: Usage) -> float | None:
        p = ANTHROPIC_PRICES.get(self.model)
        if not p:
            return None
        return (usage.input * p["input"] + usage.output * p["output"]
                + usage.cache_write * p["cache_write"] + usage.cache_read * p["cache_read"]) / 1e6


# ---------------------------------------------------------------- free cloud (OpenAI-compatible)

class _Busy(Exception):
    """The host says the model is overloaded (503); worth waiting or switching model."""


class _TooLarge(Exception):
    """Groq's 413: the request (prompt + answer allowance) is over the per-minute cap."""

    def __init__(self, over: int):
        super().__init__(f"{over} tokens over")
        self.over = max(over, 100)


def _find(obj, key: str) -> list:
    """All values for `key` anywhere inside a nested JSON-like structure."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            found += [v] if k == key else []
            found += _find(v, key)
    elif isinstance(obj, list):
        for v in obj:
            found += _find(v, key)
    return found


def _retry_after(e: "openai.RateLimitError") -> float | None:
    """Seconds to wait before this model works again.

    Gemini puts it in the error body (`retryDelay`, and a `quotaId` naming a per-day limit);
    Groq uses response headers. A per-day limit returns a large number, so callers move on.
    """
    body = getattr(e, "body", None)
    delays = [str(d) for d in _find(body, "retryDelay")]
    if delays:
        try:
            return float(delays[0].rstrip("s"))
        except ValueError:
            pass
    if any("PerDay" in str(q) for q in _find(body, "quotaId")):
        return 6 * 3600.0
    headers = getattr(e.response, "headers", {}) or {}
    for key in ("retry-after", "x-ratelimit-reset-tokens"):
        value = headers.get(key)
        if not value:
            continue
        try:
            return float(value.rstrip("s"))
        except ValueError:
            # Groq formats resets like "7.66s" or "1m2.5s".
            if "m" in value:
                mins, _, secs = value.partition("m")
                try:
                    return float(mins) * 60 + float(secs.rstrip("s") or 0)
                except ValueError:
                    pass
    return 60.0


def _is_bad_tool_call(message: str) -> bool:
    """Groq validates tool calls server-side and rejects the turn when they're malformed."""
    m = message.lower()
    return any(k in m for k in ("tool call", "call a function", "tool_use_failed", "parse tool"))


FREE_CLOUD = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "signup": "https://console.groq.com/keys",
        "default_model": "openai/gpt-oss-120b",
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "key_env": "CEREBRAS_API_KEY",
        "signup": "https://cloud.cerebras.ai",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": "GEMINI_API_KEY",
        "signup": "https://aistudio.google.com/apikey",
        # Google's free tier counts tokens far more generously than Groq's 8,000/minute,
        # so Gemini gets the full instructions and room for bigger designs.
        "tight_budget": False,
        "default_model": "gemini-3.5-flash",
    },
}


class FreeCloudProvider:
    """Open models on a free cloud tier, through the OpenAI-compatible chat API."""

    supports_research = False  # web search is Anthropic's server tool
    compact_prompt = True  # free tiers allow only ~8,000 tokens per minute
    inspect_rounds = 2  # free models benefit most from geometry feedback
    max_tokens = 5500  # leaves room for the prompt inside the per-minute token budget
    max_cooldowns = 3  # times we wait out a per-minute rate limit before giving up
    shrink = 0  # extra tokens to trim from the answer allowance after a "request too large"

    def __init__(self, name: str, model: str | None):
        cfg = FREE_CLOUD[name]
        config.load_env()
        key = os.environ.get(cfg["key_env"])
        self.hint = f"Get a free key at {cfg['signup']} and add {cfg['key_env']}=... to .env"
        if not key:
            raise ForgeError(f"{name} isn't set up. {self.hint}")
        self.name = name
        self.tight = cfg.get("tight_budget", True)
        if not self.tight:
            self.compact_prompt = False
            self.max_tokens = 16000
        # Fail fast when a free model is overloaded (Google returns 503 "high demand") instead of
        # sitting through long retries that look like a hang.
        self.client = openai.OpenAI(api_key=key, base_url=cfg["base_url"], timeout=120, max_retries=0)
        self.model = model or cfg.get("default_model", "")
        self.messages: list[dict] = []

    def list_models(self) -> list[str]:
        return sorted(m.id for m in self._call(self.client.models.list))

    def start(self, system: str, user_text: str) -> None:
        if not self.model:
            raise ForgeError(f"Choose a {self.name} model with --model. See them with: ring models --provider {self.name}")
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": user_text}]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for r in results:
            content = f"ERROR: {r.content}" if r.is_error else r.content
            self.messages.append({"role": "tool", "tool_call_id": r.id, "content": content})

    @staticmethod
    def _tools(tools: list[dict]) -> list[dict]:
        return [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"],
        }} for t in tools]

    def _call(self, fn, *args, **kwargs):
        """Run an API call, turning API errors into plain-English ForgeErrors."""
        try:
            return fn(*args, **kwargs)
        except openai.AuthenticationError:
            raise ForgeError(f"{self.name} rejected the API key. {self.hint}")
        except openai.RateLimitError:
            raise  # handled in turn(): wait out the per-minute window and retry
        except openai.NotFoundError:
            raise ForgeError(f"Model '{self.model}' isn't available on {self.name}. "
                             f"See the list with: ring models --provider {self.name}")
        except openai.APIConnectionError:
            raise ForgeError(f"Couldn't reach {self.name}. Check your internet connection.")
        except openai.APIStatusError as e:
            if e.status_code == 400 and _is_bad_tool_call(str(e.message)):
                raise MalformedToolInput() from e
            if e.status_code in (500, 502, 503, 504):
                raise _Busy() from e  # host overloaded or hiccuping: temporary
            if e.status_code == 413:
                m = re.search(r"Limit (\d+), Requested (\d+)", str(e.message))
                raise _TooLarge(int(m.group(2)) - int(m.group(1)) if m else 1000) from e
            raise ForgeError(f"{self.name} error {e.status_code}: {e.message}")
        except openai.APIError as e:
            # Errors reported mid-stream (e.g. Groq rejecting malformed tool-call JSON).
            if _is_bad_tool_call(str(e)):
                raise MalformedToolInput() from e
            raise ForgeError(f"{self.name} error: {e}")

    def _answer_budget(self) -> int:
        """Groq counts prompt + max answer length against the 8,000 tokens/minute cap, so the
        answer allowance shrinks as the prompt grows. JSON-heavy text is ~3 characters per token;
        `shrink` grows when Groq reports a request was still too large."""
        if not self.tight:
            return self.max_tokens
        prompt_tokens = sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls", "")))
                            for m in self._pruned()) / 3.0
        return int(max(1500, min(self.max_tokens, 7500 - prompt_tokens - self.shrink)))

    def _pruned(self) -> list[dict]:
        """System + request + only the latest exchange, to stay inside the token budget.

        Earlier drafts and their errors are dropped. Even the latest draft is replaced by a short
        note: the model rewrites the full spec anyway, and the check results (which name the
        exact problems) are what it needs to fix it. That saves ~3,000 tokens per retry.
        """
        head = self.messages[:2]
        tail = self.messages[2:]
        last_assistant = max((i for i, m in enumerate(tail) if m["role"] == "assistant"), default=None)
        tail = tail[last_assistant:] if last_assistant is not None else tail
        out = []
        for m in tail:
            if m["role"] == "assistant" and m.get("tool_calls"):
                calls = []
                for c in m["tool_calls"]:
                    args = c["function"]["arguments"]
                    if len(args) > 1500:
                        args = json.dumps({"spec": "(your previous draft, omitted to save space; "
                                                   "the check results below describe its problems)"})
                    calls.append({**c, "function": {**c["function"], "arguments": args}})
                m = {**m, "tool_calls": calls}
            out.append(m)
        return head + out

    def turn(self, tools: list[dict], emit: Emit) -> Turn:
        busy_waits = [3]  # one short retry; the free chain has other models to switch to
        cooldowns = 0
        for attempt in range(20):
            try:
                return self._turn(tools, emit)
            except _Busy:
                if busy_waits:
                    wait = busy_waits.pop(0)
                    emit("cooldown", seconds=wait)
                    time.sleep(wait)
                    continue
                raise ModelUnavailable(f"{self.model} is overloaded right now", self.model, 300)
            except _TooLarge as e:
                # Groq says exactly how far over we were; trim the answer allowance and retry.
                if self.shrink > 4000:
                    raise ModelUnavailable(f"{self.model}: requests too large for its free tier", self.model, 60)
                self.shrink += e.over + 300
                emit("retry", reason=f"request {e.over} tokens over the free limit, trimming and retrying")
            except openai.RateLimitError as e:
                wait = _retry_after(e) or 60.0
                if wait > 90:  # a daily limit: no point waiting, move on to another model
                    hours = wait / 3600
                    raise ModelUnavailable(f"{self.model} has used its free allowance for today "
                                           f"(resets in ~{hours:.0f} h)" if hours >= 1 else
                                           f"{self.model} is rate-limited for {wait / 60:.0f} min",
                                           self.model, wait)
                cooldowns += 1
                if cooldowns > self.max_cooldowns:
                    raise ModelUnavailable(f"{self.model} keeps hitting its per-minute limit", self.model, 120)
                emit("cooldown", seconds=round(wait))
                time.sleep(wait + 1)
        raise ModelUnavailable(f"{self.model} kept failing", self.model, 120)

    def _turn(self, tools: list[dict], emit: Emit) -> Turn:
        text, calls, finish, usage_raw = "", {}, None, None

        def run():
            nonlocal text, finish, usage_raw
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=self._pruned(),
                tools=self._tools(tools),
                max_completion_tokens=self._answer_budget(),
                stream=True,
                stream_options={"include_usage": True},
            )
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage_raw = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta and delta.content:
                    text += delta.content
                    emit("text", text=delta.content)
                for tc in (delta.tool_calls or []) if delta else []:
                    c = calls.setdefault(tc.index, {"id": None, "name": "", "args": "", "extra": {}})
                    if tc.id:
                        c["id"] = tc.id
                    # Host-specific fields on the call (e.g. Gemini's thought_signature in
                    # extra_content) must be sent back unchanged on the next turn.
                    for k, v in (getattr(tc, "model_extra", None) or {}).items():
                        if v is not None:
                            c["extra"][k] = v
                    if tc.function and tc.function.name and not c["name"]:
                        c["name"] = tc.function.name
                        emit("tool_start", name=c["name"])
                    if tc.function and tc.function.arguments:
                        c["args"] += tc.function.arguments
                        emit("tool_progress", name=c["name"], chars=len(c["args"]))
                if choice.finish_reason:
                    finish = choice.finish_reason

        self._call(run)

        tool_calls = []
        extras = {}
        for i, c in sorted(calls.items()):
            try:
                args = json.loads(c["args"] or "{}")
                args = args if isinstance(args, dict) else None
            except json.JSONDecodeError:
                args = None
            tool_calls.append(ToolCall(c["id"] or f"call_{len(self.messages)}_{i}", c["name"], args, c["args"]))
            extras[tool_calls[-1].id] = c["extra"]

        assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls:
            assistant["tool_calls"] = [{"id": t.id, "type": "function",
                                        "function": {"name": t.name, "arguments": t.raw or "{}"},
                                        **extras.get(t.id, {})}
                                       for t in tool_calls]
        self.messages.append(assistant)

        usage = Usage(turns=1)
        if usage_raw:
            usage.input = usage_raw.prompt_tokens or 0
            usage.output = usage_raw.completion_tokens or 0

        stop = {"tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}.get(finish or "", "end")
        if tool_calls and stop == "end":
            stop = "tool_use"  # some servers report "stop" even when they called tools
        return Turn(text, tool_calls, stop, usage)

    def cost(self, usage: Usage) -> float | None:
        return 0.0  # free tier


# ---------------------------------------------------------------- factory

PROVIDERS = ["anthropic", "free", *FREE_CLOUD]


def make_provider(name: str | None, model: str | None, effort: str, research: bool):
    name = name or config.provider()
    if name == "anthropic":
        return AnthropicProvider(model or config.model(), effort, research)
    if name == "free":
        raise ForgeError("The free chain is handled by forge(); pick a specific provider here.")
    if name not in FREE_CLOUD:
        raise ForgeError(f"Unknown provider '{name}'. Choose one of: {', '.join(PROVIDERS)}")
    return FreeCloudProvider(name, model or config.provider_model())
