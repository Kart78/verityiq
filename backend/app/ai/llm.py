"""Optional LLM assistance (OpenAI-compatible or Anthropic). Disabled unless configured.

Data-minimisation rule: only metadata (KPI definitions, measure names, DAX text, test
summaries, aggregate numbers) is ever sent. Raw source records are never included.
Everything returned is stored and displayed as an AI SUGGESTION.
"""
from __future__ import annotations

import os
from typing import Optional

import requests


def provider() -> Optional[str]:
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def status() -> dict:
    p = provider()
    return {"enabled": bool(p), "provider": p,
            "model": os.environ.get("LLM_MODEL") if p else None,
            "policy": "Metadata only - no raw records are sent. Outputs are labelled as suggestions."}


def complete(prompt: str, system: str = "You are a BI quality engineer. Be concise and factual.") -> str:
    p = provider()
    if p == "openai":
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        r = requests.post(f"{base}/chat/completions", timeout=60,
                          headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                          json={"model": os.environ.get("LLM_MODEL", "gpt-4o-mini"),
                                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    if p == "anthropic":
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                          headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                          json={"model": os.environ.get("LLM_MODEL", "claude-sonnet-5-5"), "max_tokens": 800,
                                "system": system, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["content"][0]["text"]
    raise RuntimeError("No LLM provider configured")


def explain_failure(test: dict, result: dict, dax: Optional[str]) -> str:
    """Metadata-only prompt for a failure explanation."""
    prompt = (f"Test: {test['name']}\nObjective: {test['business_objective']}\nSource mapping: {test['source_mapping']}\n"
              f"Target mapping: {test['target_mapping']}\nExpected (aggregate): {result.get('expected')}\n"
              f"Actual (aggregate): {result.get('actual')}\nMessage: {result.get('message')}\n"
              f"DAX: {dax or 'n/a'}\n\nGive up to 3 likely root causes and the next investigation step. "
              f"Label them as hypotheses.")
    return "[AI suggestion - unconfirmed] " + complete(prompt)
