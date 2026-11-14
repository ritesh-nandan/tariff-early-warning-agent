"""Thin Anthropic wrapper: strict-JSON calls with validation, one retry, cache and log.

Design choices that keep this working unchanged on any Claude model:
  * plain Messages API call, no `thinking`, `temperature` or `effort` parameters
    (their accepted values differ between model generations);
  * JSON is requested in the prompt and validated locally with pydantic rather than
    relying on structured-output features that older models lack.
Results are cached by a hash of (task, prompt version, model, prompt content), so a rerun
on unchanged documents costs nothing. Every call, cached or not, is logged to logs/.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

from . import config, db

T = TypeVar("T", bound=BaseModel)
LOG_FILE = config.LOGS / "llm_calls.jsonl"
MAX_TOKENS = 16000

# Newer models can decline a request via stop_reason "refusal"; a server-side fallback
# lets another Claude model answer instead. It is a beta feature, so it is tried once and
# switched off for the rest of the run if the API or model rejects it.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = ("claude-fable-5", "claude-opus-5-5", "claude-sonnet-5-5")
_state = {"client": None, "use_fallback": True}


class LLMOutputError(RuntimeError):
    """The model did not return valid JSON for the schema after one retry."""


def _client():
    if _state["client"] is None:
        import anthropic

        _state["client"] = anthropic.Anthropic()  # credentials come from the environment
    return _state["client"]


def log_call(**fields) -> None:
    config.ensure_dirs()
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": db.now(), **fields}) + "\n")


def extract_json(text: str) -> dict:
    """Parse a JSON object from model text, tolerating code fences and stray prose."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(t[start : end + 1])


def _create(model: str, system: str, messages: list[dict]):
    import anthropic

    client = _client()
    kwargs = dict(model=model, max_tokens=MAX_TOKENS, system=system, messages=messages)
    if _state["use_fallback"] and model.startswith(_FALLBACK_MODELS):
        try:
            return client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **kwargs)
        except (anthropic.BadRequestError, anthropic.NotFoundError, TypeError):
            _state["use_fallback"] = False
    return client.messages.create(**kwargs)


def call_json(task: str, doc_id: str, system: str, user: str, schema: Type[T], con,
              prompt_version: str = "v1", model: str | None = None) -> T:
    """Return a validated `schema` instance. Raises LLMOutputError after one failed retry."""
    model = model or config.MODEL
    content_hash = hashlib.sha256((system + "\x00" + user).encode("utf-8")).hexdigest()
    key = hashlib.sha256(f"{task}|{prompt_version}|{model}|{content_hash}".encode()).hexdigest()

    row = con.execute("SELECT response FROM llm_cache WHERE key=?", (key,)).fetchone()
    if row:
        try:
            out = schema.model_validate(extract_json(row["response"]))
            log_call(task=task, doc_id=doc_id, model=model, cached=True, input_tokens=0, output_tokens=0)
            return out
        except (ValidationError, json.JSONDecodeError):
            con.execute("DELETE FROM llm_cache WHERE key=?", (key,))

    messages: list[dict] = [{"role": "user", "content": user}]
    last_error = ""
    for attempt in (1, 2):
        resp = _create(model, system, messages)
        text = "".join(b.text for b in resp.content if b.type == "text")
        usage = resp.usage
        log_call(
            task=task, doc_id=doc_id, model=resp.model, requested_model=model, cached=False,
            attempt=attempt, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            stop_reason=resp.stop_reason, request_id=getattr(resp, "_request_id", None),
        )
        if resp.stop_reason == "refusal":
            last_error = "model declined the request"
            break
        try:
            out = schema.model_validate(extract_json(text))
        except (ValidationError, json.JSONDecodeError) as exc:
            last_error = str(exc)[:1500]
            messages = [
                {"role": "user", "content": user},
                {"role": "assistant", "content": text or "(empty)"},
                {"role": "user", "content": (
                    "That output was not valid for the required schema:\n" + last_error +
                    "\nReturn the corrected JSON object only, with no other text."
                )},
            ]
            continue
        db.upsert(con, "llm_cache", {
            "key": key, "task": task, "doc_id": doc_id, "model": model,
            "response": out.model_dump_json(), "created_at": db.now(),
        })
        con.commit()
        return out
    log_call(task=task, doc_id=doc_id, model=model, cached=False, error=last_error[:300])
    raise LLMOutputError(f"{task}/{doc_id}: {last_error}")
