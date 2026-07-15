"""LLM client -> local vLLM (Qwen3.6-35B, OpenAI-compatible).

Provides plain chat and *structured* chat: the latter uses vLLM guided decoding
(`guided_json`) to force the model to emit JSON matching a schema, so parsing is
reliable rather than best-effort regex on free text.
"""
from __future__ import annotations

import json

from openai import BadRequestError, OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import CONFIG

_client = OpenAI(base_url=CONFIG.llm_base_url, api_key="not-needed-local")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30))
def _create(_client_override=None, **kwargs):
    """Chat completion with retry on transient (network) errors only."""
    return (_client_override or _client).chat.completions.create(**kwargs)


_clients: dict[str, OpenAI] = {}


def _client_for(base_url: str | None) -> OpenAI:
    """Reuse one OpenAI client per base_url (e.g. the v4 endpoint on :8013)."""
    if not base_url or base_url == CONFIG.llm_base_url:
        return _client
    if base_url not in _clients:
        _clients[base_url] = OpenAI(base_url=base_url, api_key="not-needed-local")
    return _clients[base_url]


def chat(system: str, user: str, temperature: float = 0.1, max_tokens: int = 2048,
         base_url: str | None = None, model: str | None = None) -> str:
    """Plain chat. `base_url`/`model` override the default endpoint (used to reach the v4
    delex adapter on its own port while extraction/DD stay on the main model)."""
    resp = _create(
        _client_override=_client_for(base_url),
        model=model or CONFIG.llm_model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""


def chat_json(system: str, user: str, schema: dict,
              temperature: float = 0.0, max_tokens: int = 4096) -> dict:
    """Return a dict matching `schema` (JSON Schema) via structured decoding.

    Tries the OpenAI-standard response_format first, then vLLM's guided_json, so it
    works across vLLM versions. Both constrain the tokens to valid JSON."""
    base = dict(
        model=CONFIG.llm_model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    variants = [
        {"response_format": {"type": "json_schema",
                             "json_schema": {"name": "result", "schema": schema}}},
        {"extra_body": {"guided_json": schema}},
    ]
    last_err: Exception | None = None
    for extra in variants:
        try:
            resp = _create(**base, **extra)
            return json.loads(resp.choices[0].message.content)
        except BadRequestError as e:  # method unsupported -> try the next
            last_err = e
    raise RuntimeError(f"structured output unsupported by server: {last_err}")
