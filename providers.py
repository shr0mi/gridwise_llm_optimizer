"""Model providers behind a single interface.

:mod:`llm` owns everything that makes the interpretation path survive a bad day
-- key pool, circuit breaker, model chain, note cache, cross-check, arbiter and
the deterministic guardrails. This module owns only "turn a prompt plus a JSON
schema into parsed JSON", once per provider, so that machinery applies to
whichever provider is selected by ``LLM_PROVIDER``.

Two providers ship:

``gemini``     Google AI Studio via ``google-genai``. Free tier is enough for
               this round; quotas are per model and per project.
``anthropic``  Claude via the ``anthropic`` SDK, for when Gemini is unavailable
               or a project is blocked. Uses structured outputs
               (``output_config.format``) rather than forced tool choice.

Clients are cached per (api key, event loop). An async client binds to the loop
that created it and raises if reused from another, which bites whenever tests
call ``asyncio.run`` more than once in a process.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional, Set

log = logging.getLogger("gridwise.providers")

# The Gemini SDK warns about automatic function calling on every call. We send
# no tools, so it is noise that would bury the signals worth reading.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)


class ProviderUnavailable(RuntimeError):
    """The provider SDK is missing or a client could not be constructed."""


def _loop_id() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def parse_payload(text: str, *keys: str) -> Any:
    """Defensive JSON parse. Structured output should not need the fallbacks."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    if isinstance(data, dict):
        for key in keys:
            if key in data:
                return data[key]
        return next(iter(data.values()), None) if len(data) == 1 else None
    return data


# ----------------------------------------------------------------------- Gemini


class GeminiProvider:
    name = "gemini"
    key_envs = ("GEMINI_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    default_model = "gemini-2.5-flash"
    #: Probed against a live free-tier key. `gemini-flash-latest` is deliberately
    #: absent: it returns 429 at the same moment as `gemini-2.5-flash`, i.e. it
    #: shares that quota bucket, so it is worthless as a fallback. Also absent:
    #: `gemini-2.5-flash-lite`, `gemini-2.0-flash` and `gemini-2.5-pro`, which
    #: return 404 "no longer available to new users" for newly created keys.
    #: Ordered by MEASURED latency against the real interpretation prompt, not by
    #: model tier: gemini-3.1-flash-lite answered in 1.8s, gemini-3-flash-preview
    #: in 9.4s -- too slow for the interpretation budget, so it is not here.
    default_fallbacks = ("gemini-3.1-flash-lite", "gemini-3.5-flash",
                         "gemini-3.5-flash-lite")

    def __init__(self) -> None:
        self._clients: Dict[tuple, Any] = {}
        self._import_failed = False

    def client(self, key: str):
        cache_key = (key, _loop_id())
        if cache_key in self._clients:
            return self._clients[cache_key]
        if self._import_failed:
            raise ProviderUnavailable("google-genai is not importable")
        try:
            from google import genai
            client = genai.Client(api_key=key)
        except ImportError as exc:
            self._import_failed = True
            raise ProviderUnavailable("google-genai is not installed") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable("could not construct the Gemini client") from exc
        self._clients[cache_key] = client
        return client

    async def generate(self, model: str, key: str, prompt: str,
                       schema: Dict[str, Any], system: str,
                       no_thinking: Set[str]) -> Any:
        from google.genai import types

        client = self.client(key)
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=schema,
            safety_settings=[
                types.SafetySetting(category=c, threshold="BLOCK_NONE")
                for c in ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                          "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                          "HARM_CATEGORY_DANGEROUS_CONTENT")
            ],
        )
        # Thinking off on extraction: it costs seconds of latency and free-tier
        # tokens for a task that does not need it. Some models reject the field
        # outright; those are remembered by the caller and skipped.
        if model not in no_thinking:
            try:
                config.thinking_config = types.ThinkingConfig(thinking_budget=0)
            except Exception:  # noqa: BLE001
                pass

        resp = await client.aio.models.generate_content(
            model=model, contents=prompt, config=config)
        return parse_payload(getattr(resp, "text", "") or "",
                             "interpretations", "choices")


# -------------------------------------------------------------------- Anthropic


def _to_json_schema(gemini_schema: Dict[str, Any]) -> Dict[str, Any]:
    """Translate the Gemini-flavoured schema into plain JSON Schema."""
    if not isinstance(gemini_schema, dict):
        return {"type": "object"}
    out: Dict[str, Any] = {}
    for key, value in gemini_schema.items():
        if key == "type" and isinstance(value, str):
            out["type"] = value.lower()
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {k: _to_json_schema(v) for k, v in value.items()}
        elif key == "items":
            out["items"] = _to_json_schema(value)
        elif key == "nullable":
            continue
        else:
            out[key] = value
    if out.get("type") == "object" and "properties" in out:
        out.setdefault("additionalProperties", False)
    return out


class AnthropicProvider:
    name = "anthropic"
    key_envs = ("ANTHROPIC_API_KEYS", "ANTHROPIC_API_KEY")
    default_model = "claude-haiku-4-5"
    default_fallbacks = ("claude-sonnet-5",)

    def __init__(self) -> None:
        self._clients: Dict[tuple, Any] = {}
        self._import_failed = False

    def client(self, key: str):
        cache_key = (key, _loop_id())
        if cache_key in self._clients:
            return self._clients[cache_key]
        if self._import_failed:
            raise ProviderUnavailable("anthropic is not importable")
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=key, max_retries=0)
        except ImportError as exc:
            self._import_failed = True
            raise ProviderUnavailable("anthropic is not installed") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable("could not construct the Anthropic client") from exc
        self._clients[cache_key] = client
        return client

    async def generate(self, model: str, key: str, prompt: str,
                       schema: Dict[str, Any], system: str,
                       no_thinking: Set[str]) -> Any:
        client = self.client(key)
        # Structured outputs rather than forced tool choice: it is the supported
        # way to guarantee the envelope, and forced tool_choice is rejected on
        # some current models.
        #
        # `output_config.effort` is deliberately NOT sent. It is rejected with a
        # 400 by Haiku 4.5 and Sonnet 4.5, and sending it unconditionally turns
        # every request into an error. Extraction does not need thinking either,
        # so no `thinking` block is sent.
        resp = await client.messages.create(
            model=model,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": _to_json_schema(schema),
                }
            },
        )
        if getattr(resp, "stop_reason", None) == "refusal":
            raise RuntimeError("model refused the interpretation request")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return parse_payload(text, "interpretations", "choices")


_REGISTRY = {"gemini": GeminiProvider, "anthropic": AnthropicProvider}
_INSTANCES: Dict[str, Any] = {}


def get_provider(name: Optional[str] = None):
    key = (name or "gemini").strip().lower()
    if key not in _REGISTRY:
        log.warning("unknown LLM_PROVIDER %r; falling back to gemini", key)
        key = "gemini"
    if key not in _INSTANCES:
        _INSTANCES[key] = _REGISTRY[key]()
    return _INSTANCES[key]


def reset() -> None:
    """Drop cached provider instances and their clients (tests)."""
    _INSTANCES.clear()
