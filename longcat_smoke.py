# ==================== LongCat API Key Smoke Test ====================
#
# Tests whether a generated LongCat API key works for a basic chat request.
# Endpoint is documented at: https://longcat.chat/platform/docs/
#
# OpenAI-compatible:
#   POST https://api.longcat.chat/openai/v1/chat/completions
#
# Usage (PowerShell):
#   $env:LONGCAT_API_KEY="ak_..."; python longcat_smoke.py
#
# Proxies are intentionally not supported in this trimmed repo.
#
# We intentionally avoid printing the full API key.

from __future__ import annotations

import os
import sys
import json
import requests

CHAT_COMPLETIONS_URL = "https://api.longcat.chat/openai/v1/chat/completions"
MODELS_URL = "https://api.longcat.chat/openai/v1/models"


def _redact_key(key: str) -> str:
    if not key:
        return ""
    key = str(key)
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def test_longcat_api_key(
    api_key: str,
    model: str = "LongCat-Flash-Chat",
    # Default to a simple Chinese prompt (user request) without introducing non-ASCII.
    prompt: str = "\u4f60\u597d",
    timeout_s: int = 30,
) -> dict:
    s = requests.Session()
    s.trust_env = False
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # 1) optional: list models to verify auth
    models_ok = None
    try:
        r = s.get(MODELS_URL, headers=headers, timeout=timeout_s)
        models_ok = r.status_code == 200
    except Exception:
        models_ok = None

    # 2) chat request
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 128,
    }
    r = s.post(CHAT_COMPLETIONS_URL, headers=headers, json=payload, timeout=timeout_s)
    text = r.text
    try:
        data = r.json()
    except Exception:
        data = None

    if r.status_code != 200:
        return {
            "ok": False,
            "status_code": r.status_code,
            "models_ok": models_ok,
            "error_body": text[:500],
        }

    # OpenAI chat.completions style:
    # data.choices[0].message.content
    content = None
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        content = None

    return {
        "ok": True,
        "status_code": r.status_code,
        "models_ok": models_ok,
        "reply": content,
        "raw": data,
    }


def main() -> int:
    api_key = os.getenv("LONGCAT_API_KEY", "").strip()
    if not api_key:
        print("Missing env LONGCAT_API_KEY (e.g. ak_...)", file=sys.stderr)
        return 2

    print(f"Testing LongCat API key: {_redact_key(api_key)}")
    result = test_longcat_api_key(api_key=api_key)
    if result.get("ok"):
        reply = result.get("reply") or ""
        print("OK: chat request succeeded")
        if reply:
            print("Assistant:", reply)
        return 0

    print("FAILED:", json.dumps(result, ensure_ascii=True, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
