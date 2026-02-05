"""
Email service (GPTMail-only).

CloudMail/KYX support was removed to simplify deployment and configuration.
All settings are read from config.toml, with optional env override:
  - GPTMAIL_API_KEY (recommended for Docker)
"""

from __future__ import annotations

import json
import random
import re
import string
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, TypeVar

import requests

from config import (
    GPTMAIL_API_BASE,
    GPTMAIL_API_KEY,
    GPTMAIL_PREFIX,
    VERIFICATION_CODE_INTERVAL,
    VERIFICATION_CODE_MAX_RETRIES,
    REQUEST_TIMEOUT,
    get_random_gptmail_domain,
)
from logger import log


T = TypeVar("T")


@dataclass
class PollResult:
    success: bool
    data: Any = None
    error: str = ""


def poll_with_retry(
    *,
    fetch_func: Callable[[], T],
    check_func: Callable[[T], Optional[str]],
    max_retries: int,
    interval: int,
    description: str = "poll",
) -> PollResult:
    max_retries = max(1, int(max_retries or 1))
    interval = max(1, int(interval or 1))
    last_error = ""

    for i in range(max_retries):
        try:
            data = fetch_func()
            code = check_func(data)
            if code:
                return PollResult(success=True, data=code)
        except Exception as e:
            last_error = str(e)
        time.sleep(interval)

    return PollResult(success=False, error=last_error or f"{description} timeout")


class GPTMailService:
    """GPTMail temporary email service."""

    def __init__(self, api_base: str | None = None, api_key: str | None = None):
        self.api_base = (api_base or GPTMAIL_API_BASE).rstrip("/")
        self.api_key = (api_key or GPTMAIL_API_KEY).strip()
        # Keep headers minimal; never log api_key. Always prefer JSON responses.
        self.headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        self._session = requests.Session()
        self._session.trust_env = False

    @staticmethod
    def _safe_json_loads(text: str) -> Any:
        """
        Tolerant JSON parser.

        Some deployments may return multiple JSON values in one body (e.g. `null\\n{...}`),
        which breaks requests' `Response.json()` with `JSONDecodeError: Extra data`.

        This function accepts:
          - standard single JSON document
          - optional XSSI prefix `)]}',`
          - multiple concatenated JSON values (returns the last parsed value)
        """
        s = (text or "").strip()
        if not s:
            raise ValueError("empty response body")

        if s.startswith(")]}',"):
            s = s.split("\n", 1)[1] if "\n" in s else s[5:]
            s = s.strip()

        dec = json.JSONDecoder()
        idx = 0
        last = None
        while idx < len(s):
            while idx < len(s) and s[idx].isspace():
                idx += 1
            if idx >= len(s):
                break
            last, end = dec.raw_decode(s, idx)
            idx = end
        return last

    def _parse_json_response(self, r: requests.Response) -> dict[str, Any]:
        text = r.text or ""
        if r.status_code >= 400:
            raise RuntimeError(f"GPTMail HTTP {r.status_code}: {text[:300]}")

        try:
            data = r.json()
        except Exception:
            try:
                data = self._safe_json_loads(text)
            except Exception as e:
                ct = (r.headers.get("content-type") or "").strip()
                raise RuntimeError(f"GPTMail invalid JSON (ct={ct}): {text[:300]}") from e

        if data is None:
            return {}
        if not isinstance(data, dict):
            raise RuntimeError(f"GPTMail API returned non-object JSON: {type(data).__name__}")
        return data

    def generate_email(self, *, prefix: str | None = None, domain: str | None = None) -> tuple[str | None, str | None]:
        url = f"{self.api_base}/api/generate-email"
        try:
            headers = dict(self.headers)
            if prefix or domain:
                payload: dict[str, str] = {}
                if prefix:
                    payload["prefix"] = prefix
                if domain:
                    payload["domain"] = domain
                headers["Content-Type"] = "application/json"
                r = self._session.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
            else:
                r = self._session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

            data = self._parse_json_response(r)
            if data.get("success"):
                email = (data.get("data") or {}).get("email", "")
                email = str(email).strip()
                if email:
                    log.success(f"GPTMail 生成邮箱: {email}")
                    return email, None
            return None, str(data.get("error") or data.get("message") or "generate-email failed")
        except Exception as e:
            return None, str(e)

    def get_emails(self, email: str) -> tuple[list[dict[str, Any]] | None, str | None]:
        # GPTMail API endpoint naming has changed over time:
        # - newer: /api/emails
        # - older: /api/get-emails
        urls = [f"{self.api_base}/api/emails", f"{self.api_base}/api/get-emails"]
        try:
            last_err = None
            for url in urls:
                try:
                    r = self._session.get(
                        url, headers=self.headers, params={"email": email}, timeout=REQUEST_TIMEOUT
                    )
                    data = self._parse_json_response(r)
                    if data.get("success"):
                        items = (data.get("data") or {}).get("emails") or []
                        return items if isinstance(items, list) else [], None
                    last_err = str(data.get("error") or data.get("message") or "get-emails failed")
                except Exception as e:
                    last_err = str(e)
            return None, last_err or "get-emails failed"
        except Exception as e:
            return None, str(e)

    @staticmethod
    def _extract_code(text: str) -> str | None:
        if not text:
            return None
        # 4-8 digits OTP; use word boundaries to avoid picking up timestamps, etc.
        m = re.search(r"\b(\d{4,8})\b", text)
        return m.group(1) if m else None

    def get_verification_code(
        self, email: str, max_retries: int | None = None, interval: int | None = None
    ) -> tuple[str | None, str | None, str | None]:
        """
        Poll GPTMail inbox and extract a 4-8 digit OTP.

        We log lightweight progress every few polls so users can tell whether we are:
          - actually receiving emails but failing to match the code
          - not receiving anything at all
          - failing to talk to GPTMail (auth/format errors)
        """
        max_retries = int(max_retries or VERIFICATION_CODE_MAX_RETRIES)
        interval = int(interval or VERIFICATION_CODE_INTERVAL)

        last_error = ""
        last_time: str | None = None
        last_count = 0

        for i in range(max_retries):
            emails: list[dict[str, Any]] = []
            err = None
            try:
                emails, err = self.get_emails(email)
            except Exception as e:  # pragma: no cover
                err = str(e)

            if err:
                last_error = str(err)
                # Throttle warnings to avoid spamming logs.
                if i == 0 or (i + 1) % 5 == 0:
                    log.warning(f"GPTMail inbox poll error ({i + 1}/{max_retries}): {last_error}")
                time.sleep(interval)
                continue

            items = emails or []
            last_count = len(items)

            for item in items:
                subj = str(item.get("subject") or "")
                content = str(item.get("content") or "")
                last_time = str(item.get("created_at") or item.get("date") or "") or last_time
                code = self._extract_code(subj) or self._extract_code(content)
                if code:
                    log.success(f"GPTMail 验证码获取成功: {code}")
                    return str(code), None, last_time

            # Progress log (every ~5 polls).
            if i == 0 or (i + 1) % 5 == 0:
                if last_count == 0:
                    log.info(f"GPTMail inbox empty ({i + 1}/{max_retries})")
                else:
                    # Print the newest subject snippet for troubleshooting (no secrets).
                    newest = items[0] if isinstance(items[0], dict) else {}
                    subj = str(newest.get("subject") or "")[:120]
                    log.info(f"GPTMail inbox has {last_count} email(s) ({i + 1}/{max_retries}), newest subject: {subj}")

            time.sleep(interval)

        return None, last_error or "未能获取验证码", last_time


gptmail_service = GPTMailService()


def unified_create_email() -> tuple[str | None, str | None]:
    """
    Create an email for OTP login.

    Returns:
      (email, password) - password is always None for GPTMail.
    """
    random_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    prefix = (GPTMAIL_PREFIX or "").strip() or f"{random_str}-lc"
    domain = get_random_gptmail_domain() or None
    email, err = gptmail_service.generate_email(prefix=prefix, domain=domain)
    if not email:
        log.error(f"GPTMail 生成邮箱失败: {err}")
        return None, None
    return email, None


def unified_get_verification_code(
    email: str, max_retries: int | None = None, interval: int | None = None
) -> tuple[str | None, str | None, str | None]:
    return gptmail_service.get_verification_code(email, max_retries, interval)
