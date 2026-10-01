"""Bedrock-backed incident analysis — opt-in, metered, single-call.

Cost controls, in the order they apply:

  1. Not called at all unless `--ai` is passed; `offline.narrate()` is the default.
  2. The prompt is a compacted snapshot (see prompt.py), not raw telemetry, so
     input size is bounded by the number of distinct signals, not log volume.
  3. `max_tokens` is capped low (default 900) because the output is a structured
     summary, not an essay.
  4. Cost is estimated and checked against `--max-spend` *before* the call, then
     re-charged against the real token usage the API reports back.
  5. Structured output (`output_config.format`) means one call returns parseable
     JSON — no retry loop, no "please reply in JSON" second attempt.
  6. The response is cached like any other call, so re-rendering a report from
     the same snapshot is free.

Model default is the cheapest capable Claude on Bedrock (Haiku 4.5). Override
with `--model` when an incident warrants a stronger one.
"""

from __future__ import annotations

import json
from typing import Any

from ..errors import MissingDependency, SreToolkitError
from ..models import Finding, Snapshot
from ..pricing import DEFAULT_MODEL, model_price
from . import prompt as prompt_mod


def available() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def analyse(
    ctx,
    snap: Snapshot,
    model_id: str | None = None,
    max_tokens: int = 900,
) -> dict[str, Any]:
    """Returns {'narrative': str, 'findings': [Finding], 'usage': {...}}."""
    model_id = model_id or DEFAULT_MODEL
    context = prompt_mod.build(snap)
    estimated_in = prompt_mod.estimate_tokens(prompt_mod.SYSTEM + context)

    cache_key = {
        "model": model_id, "max_tokens": max_tokens,
        "context_hash": hash(context), "schema": "v1",
    }
    cached = ctx.cache.get("bedrock:analyse", cache_key)
    if cached is not None:
        ctx.ledger.entries.append(_cached_entry(model_id, cached.get("usage", {})))
        return _shape(cached, model_id)

    # Pre-flight budget check with a pessimistic output assumption.
    ctx.ledger.charge_tokens(
        model_id, estimated_in, max_tokens, detail="pre-flight estimate"
    )
    ctx.ledger.entries.pop()  # the estimate was only for the guard rail

    if ctx.dry_run:
        from ..pricing import token_cost

        usd = token_cost(model_id, estimated_in, max_tokens)
        ctx.ledger.entries.append(
            _entry(model_id, estimated_in, max_tokens, usd, "dry-run estimate", billed=False)
        )
        return {
            "narrative": "(dry run — no model called)",
            "findings": [],
            "usage": {"input_tokens": estimated_in, "output_tokens": 0, "estimated": True},
        }

    try:
        from anthropic import AnthropicBedrockMantle
    except ImportError as exc:
        raise MissingDependency("anthropic", "ai") from exc

    client = AnthropicBedrockMantle(aws_region=ctx.aws.region)
    try:
        response = client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            system=prompt_mod.SYSTEM,
            messages=[{"role": "user", "content": context}],
            output_config={"format": {"type": "json_schema", "schema": prompt_mod.OUTPUT_SCHEMA}},
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a clean CLI message
        raise SreToolkitError(
            f"Bedrock call failed ({type(exc).__name__}): {exc}\n"
            "The deterministic analysis is still available — re-run without --ai."
        ) from exc

    usage = {
        "input_tokens": getattr(response.usage, "input_tokens", estimated_in),
        "output_tokens": getattr(response.usage, "output_tokens", 0),
    }
    ctx.ledger.charge_tokens(
        model_id, usage["input_tokens"], usage["output_tokens"], detail="incident analysis"
    )

    text = next((b.text for b in response.content if b.type == "text"), "{}")
    try:
        payload = json.loads(text)
    except ValueError:
        payload = {"summary": text.strip(), "contributing_factors": [], "next_checks": []}

    result = {"payload": payload, "usage": usage}
    ctx.cache.set("bedrock:analyse", cache_key, result)
    return _shape(result, model_id)


def _shape(result: dict, model_id: str) -> dict[str, Any]:
    payload = result.get("payload", {})
    confidence_map = {"high": 0.8, "medium": 0.55, "low": 0.3}
    findings = [
        Finding(
            title=item.get("title", "Model hypothesis"),
            confidence=confidence_map.get(item.get("confidence", "low"), 0.3),
            rationale=item.get("why", ""),
            next_steps=list(payload.get("next_checks", []))[:2],
            source=f"bedrock:{model_id}",
        )
        for item in payload.get("contributing_factors", [])[:3]
    ]
    narrative = payload.get("summary", "")
    missing = payload.get("missing_evidence") or []
    if missing:
        narrative += "\n\nEvidence that would most change this conclusion: " + "; ".join(missing)
    return {"narrative": narrative, "findings": findings, "usage": result.get("usage", {})}


def _entry(model_id, in_tokens, out_tokens, usd, detail, billed=True):
    from ..ledger import Entry

    return Entry(
        operation=f"bedrock:{model_id}",
        quantity=in_tokens + out_tokens,
        unit="tokens",
        usd=usd,
        detail=detail,
        billed=billed,
    )


def _cached_entry(model_id: str, usage: dict):
    from ..pricing import token_cost

    usd = token_cost(model_id, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
    return _entry(model_id, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                  usd, "cached analysis", billed=False)


def price_note(model_id: str | None = None) -> str:
    model_id = model_id or DEFAULT_MODEL
    price = model_price(model_id)
    return f"{model_id} — ${price['input']:.2f}/1M in, ${price['output']:.2f}/1M out"
