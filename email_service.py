"""
Email service (GPTMail + DuckMail).

CloudMail/KYX support was removed to simplify deployment and configuration.
All settings are read from config.toml, with optional env overrides:
  - GPTMAIL_API_KEY
  - duckmail_apikey
"""

from __future__ import annotations

import json
import random
import re
import secrets
import string
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, TypeVar

import requests

from config import (
    DUCKMAIL_API_BASE,
    DUCKMAIL_API_KEY,
    DUCKMAIL_PREFIX,
    EMAIL_PROVIDER,
    GPTMAIL_API_BASE,
    GPTMAIL_API_KEY,
    GPTMAIL_PREFIX,
    VERIFICATION_CODE_INTERVAL,
    VERIFICATION_CODE_MAX_RETRIES,
    REQUEST_TIMEOUT,
    get_random_duckmail_domain,
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
        try:
            headers = dict(self.headers)
            if prefix or domain:
                url = f"{self.api_base}/custom"
                payload: dict[str, str] = {"provider": "gptmail"}
                if prefix:
                    payload["prefix"] = prefix
                if domain:
                    payload["domain"] = domain
                headers["Content-Type"] = "application/json"
                r = self._session.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
            else:
                url = f"{self.api_base}/generate"
                r = self._session.get(url, params={"provider": "gptmail"}, headers=headers, timeout=REQUEST_TIMEOUT)

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
        url = f"{self.api_base}/emails/{email}"
        try:
            r = self._session.get(
                url, headers=self.headers, params={"provider": "gptmail"}, timeout=REQUEST_TIMEOUT
            )
            data = self._parse_json_response(r)
            if data.get("success"):
                items = (data.get("data") or {}).get("emails") or []
                return items if isinstance(items, list) else [], None
            return None, str(data.get("error") or data.get("message") or "get-emails failed")
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


class DuckMailService:
    """
    DuckMail temporary email service.

    API shape is similar to mail.tm:
      - POST /accounts {address,password}
      - POST /token {address,password} -> JWT token
      - GET  /messages (Authorization: Bearer ...)
      - GET  /messages/{id} (optional, full body)
      - GET  /domains

    `duckmail_apikey` is optional; when set we pass it as `X-API-Key`.
    """

    def __init__(self, api_base: str | None = None, api_key: str | None = None):
        self.api_base = (api_base or DUCKMAIL_API_BASE).rstrip("/")
        self.api_key = (api_key or DUCKMAIL_API_KEY).strip()

        self._session = requests.Session()
        self._session.trust_env = False

        # In-memory mapping for generated accounts.
        self._tokens: dict[str, str] = {}
        self._domains_cache: tuple[float, list[str]] = (0.0, [])

    def _base_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _auth_headers(self, token: str) -> dict[str, str]:
        headers = self._base_headers()
        headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _extract_code(text: str) -> str | None:
        if not text:
            return None
        m = re.search(r"\b(\d{4,8})\b", text)
        return m.group(1) if m else None

    def _safe_json(self, r: requests.Response) -> dict[str, Any]:
        try:
            data = r.json()
        except Exception:
            data = {}

        if isinstance(data, dict):
            return data
        return {}

    def _summarize_http_error(self, r: requests.Response) -> str:
        data = self._safe_json(r)
        msg = str(data.get("message") or data.get("hydra:description") or data.get("detail") or "").strip()
        if msg:
            return msg[:300]
        txt = (r.text or "").strip()
        return txt[:300] if txt else f"HTTP {r.status_code}"

    def list_domains(self, *, force: bool = False) -> tuple[list[str] | None, str | None]:
        now = time.time()
        cached_at, cached = self._domains_cache
        if not force and cached and (now - cached_at) < 600:
            return cached.copy(), None

        url = f"{self.api_base}/domains"
        try:
            r = self._session.get(url, headers=self._base_headers(), timeout=REQUEST_TIMEOUT)
            if r.status_code >= 400:
                return None, self._summarize_http_error(r)

            data = self._safe_json(r)
            items = data.get("hydra:member") or []
            domains: list[str] = []
            if isinstance(items, list):
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    if it.get("isVerified") is False:
                        continue
                    dom = str(it.get("domain") or "").strip()
                    if dom:
                        domains.append(dom)

            self._domains_cache = (now, domains)
            return domains.copy(), None
        except Exception as e:
            return None, str(e)

    def _pick_domain(self, domain: str | None = None) -> tuple[str | None, str | None]:
        if domain and str(domain).strip():
            return str(domain).strip(), None

        cfg_dom = (get_random_duckmail_domain() or "").strip()
        if cfg_dom:
            return cfg_dom, None

        domains, err = self.list_domains()
        if err:
            return None, err
        if not domains:
            return None, "DuckMail: no domains available"
        return random.choice(domains), None

    @staticmethod
    def _sanitize_local_part(prefix: str) -> str:
        s = (prefix or "").strip().lower()
        if not s:
            return ""
        s = re.sub(r"[^a-z0-9]+", "-", s)
        s = re.sub(r"-{2,}", "-", s).strip("-")
        return s[:32]

    @staticmethod
    def _random_tail(length: int = 10) -> str:
        alphabet = string.ascii_lowercase + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(max(6, int(length or 10))))

    @staticmethod
    def _random_password(length: int = 14) -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(max(10, int(length or 14))))

    def create_account(self, address: str, password: str) -> tuple[bool, str | None]:
        url = f"{self.api_base}/accounts"
        try:
            r = self._session.post(
                url,
                headers={**self._base_headers(), "Content-Type": "application/json"},
                json={"address": address, "password": password},
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code in (200, 201):
                return True, None
            return False, self._summarize_http_error(r)
        except Exception as e:
            return False, str(e)

    def login(self, address: str, password: str) -> tuple[str | None, str | None]:
        url = f"{self.api_base}/token"
        try:
            r = self._session.post(
                url,
                headers={**self._base_headers(), "Content-Type": "application/json"},
                json={"address": address, "password": password},
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code >= 400:
                return None, self._summarize_http_error(r)
            data = self._safe_json(r)
            token = str(data.get("token") or "").strip()
            if not token:
                return None, "DuckMail: missing token in response"
            self._tokens[address] = token
            return token, None
        except Exception as e:
            return None, str(e)

    def generate_email(
        self, *, prefix: str | None = None, domain: str | None = None, max_attempts: int = 8
    ) -> tuple[str | None, str | None, str | None]:
        dom, err = self._pick_domain(domain)
        if not dom:
            return None, None, err or "DuckMail: domain unavailable"

        base = self._sanitize_local_part(prefix or DUCKMAIL_PREFIX or "")
        for _ in range(max(1, int(max_attempts or 1))):
            tail = self._random_tail(10)
            local = f"{base}-{tail}" if base else f"{tail}-lc"
            address = f"{local}@{dom}"
            password = self._random_password(14)

            ok, create_err = self.create_account(address, password)
            if ok:
                token, login_err = self.login(address, password)
                if not token:
                    return None, None, login_err or "DuckMail login failed"
                log.success(f"DuckMail 生成邮箱: {address}")
                return address, password, None

            # Account collision: retry with a new random local-part.
            msg = (create_err or "").lower()
            if "already" in msg or "used" in msg or "exists" in msg:
                continue
            return None, None, create_err or "DuckMail create account failed"

        return None, None, "DuckMail create account failed (too many collisions)"

    def get_messages(self, token: str) -> tuple[list[dict[str, Any]] | None, str | None]:
        url = f"{self.api_base}/messages"
        try:
            r = self._session.get(url, headers=self._auth_headers(token), timeout=REQUEST_TIMEOUT)
            if r.status_code >= 400:
                return None, self._summarize_http_error(r)
            data = self._safe_json(r)
            items = data.get("hydra:member") or []
            return items if isinstance(items, list) else [], None
        except Exception as e:
            return None, str(e)

    def get_message(self, token: str, message_id: str) -> tuple[dict[str, Any] | None, str | None]:
        url = f"{self.api_base}/messages/{message_id}"
        try:
            r = self._session.get(url, headers=self._auth_headers(token), timeout=REQUEST_TIMEOUT)
            if r.status_code >= 400:
                return None, self._summarize_http_error(r)
            data = self._safe_json(r)
            return data, None
        except Exception as e:
            return None, str(e)

    def get_verification_code(
        self, email: str, max_retries: int | None = None, interval: int | None = None
    ) -> tuple[str | None, str | None, str | None]:
        max_retries = int(max_retries or VERIFICATION_CODE_MAX_RETRIES)
        interval = int(interval or VERIFICATION_CODE_INTERVAL)

        token = self._tokens.get(email)
        if not token:
            return None, "DuckMail: missing token for this email (create the email in this run first)", None

        last_error = ""
        last_time: str | None = None
        last_count = 0

        for i in range(max_retries):
            messages: list[dict[str, Any]] = []
            err = None
            try:
                messages, err = self.get_messages(token)
            except Exception as e:  # pragma: no cover
                err = str(e)

            if err:
                last_error = str(err)
                if i == 0 or (i + 1) % 5 == 0:
                    log.warning(f"DuckMail inbox poll error ({i + 1}/{max_retries}): {last_error}")
                time.sleep(interval)
                continue

            items = messages or []
            last_count = len(items)

            def _msg_ts(m: dict[str, Any]) -> str:
                return str(m.get("createdAt") or m.get("created_at") or m.get("date") or "")

            items_sorted = sorted(
                [m for m in items if isinstance(m, dict)],
                key=_msg_ts,
                reverse=True,
            )

            # Fast path: subject/intro snippet.
            for item in items_sorted:
                subj = str(item.get("subject") or "")
                intro = str(item.get("intro") or item.get("snippet") or "")
                last_time = _msg_ts(item) or last_time
                code = self._extract_code(subj) or self._extract_code(intro)
                if code:
                    log.success(f"DuckMail 验证码获取成功: {code}")
                    return str(code), None, last_time

            # Slow path: fetch full body for a few newest messages.
            for item in items_sorted[:3]:
                mid = str(item.get("id") or "").strip()
                if not mid:
                    continue
                detail, derr = self.get_message(token, mid)
                if derr or not isinstance(detail, dict):
                    continue
                text = str(detail.get("text") or "")
                html = detail.get("html")
                html_text = ""
                if isinstance(html, list):
                    html_text = " ".join(str(x or "") for x in html)
                elif isinstance(html, str):
                    html_text = html
                code = self._extract_code(text) or self._extract_code(html_text)
                if code:
                    log.success(f"DuckMail 验证码获取成功: {code}")
                    return str(code), None, last_time

            if i == 0 or (i + 1) % 5 == 0:
                if last_count == 0:
                    log.info(f"DuckMail inbox empty ({i + 1}/{max_retries})")
                else:
                    newest = items_sorted[0] if items_sorted else {}
                    subj = str(newest.get("subject") or "")[:120]
                    log.info(
                        f"DuckMail inbox has {last_count} message(s) ({i + 1}/{max_retries}), newest subject: {subj}"
                    )

            time.sleep(interval)

        return None, last_error or "未能获取验证码", last_time


gptmail_service = GPTMailService()
duckmail_service = DuckMailService()


def _provider() -> str:
    p = (EMAIL_PROVIDER or "gptmail").strip().lower()
    return p if p in ("gptmail", "duckmail") else "gptmail"


def unified_create_email() -> tuple[str | None, str | None]:
    """
    Create an email for OTP login.

    Returns:
      (email, password)
    """
    if _provider() == "duckmail":
        random_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        prefix = (DUCKMAIL_PREFIX or "").strip() or f"{random_str}-lc"
        domain = get_random_duckmail_domain() or None
        email, password, err = duckmail_service.generate_email(prefix=prefix, domain=domain)
        if not email:
            log.error(f"DuckMail 生成邮箱失败: {err}")
            return None, None
        return email, password

    # Default: GPTMail
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
    if _provider() == "duckmail":
        return duckmail_service.get_verification_code(email, max_retries, interval)
    return gptmail_service.get_verification_code(email, max_retries, interval)
