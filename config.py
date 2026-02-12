"""
Project configuration loader (config.toml).

This repo is intentionally trimmed to focus on:
  - LongCat (MyKeeta Passport) automation (browser-based)
  - Optional GPT-Load key sync

All configuration is read from `config.toml` (no env vars required for core features).
"""

from __future__ import annotations

import os
import random
import sys
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore


BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.toml"
CONFIG_FALLBACK_FILE = BASE_DIR / "config.toml.example"

_config_errors: list[dict] = []


def _log_config(level: str, source: str, message: str, details: str | None = None) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    full = f"[{ts}] [{level}] 配置 [{source}]: {message}"
    if details:
        full += f" - {details}"

    # Keep startup logs visible even before logger.py is imported.
    if level in ("ERROR", "WARNING"):
        print(full, file=sys.stderr)
        _config_errors.append({"level": level, "source": source, "message": message, "details": details})
    else:
        print(full)


def get_config_errors() -> list[dict]:
    return _config_errors.copy()


def _load_toml() -> dict:
    if tomllib is None:
        _log_config("ERROR", "config.toml", "tomllib/tomli 未安装", "请安装 tomli 或使用 Python 3.11+")
        return {}
    cfg_path = CONFIG_FILE
    if not cfg_path.exists():
        # Zeabur/Git-based deployments often won't include config.toml because it's gitignored by
        # default. Use the tracked template as a safe fallback.
        if CONFIG_FALLBACK_FILE.exists():
            _log_config(
                "WARNING",
                "config.toml",
                "配置文件不存在，已回退到 config.toml.example",
                str(cfg_path),
            )
            cfg_path = CONFIG_FALLBACK_FILE
        else:
            _log_config("WARNING", "config.toml", "配置文件不存在", str(cfg_path))
            return {}
    try:
        with cfg_path.open("rb") as f:
            cfg = tomllib.load(f)
        _log_config("INFO", cfg_path.name, "配置文件加载成功")
        return cfg if isinstance(cfg, dict) else {}
    except Exception as e:
        _log_config("ERROR", cfg_path.name, "加载失败", f"{type(e).__name__}: {e}")
        return {}


_cfg = _load_toml()


def _as_int(v, default: int) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


def _as_bool(v, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _as_str(v, default: str = "") -> str:
    try:
        if v is None:
            return default
        return str(v)
    except Exception:
        return default


# -------------------- Request / Verification --------------------
_req = _cfg.get("request", {}) if isinstance(_cfg, dict) else {}
REQUEST_TIMEOUT = _as_int(_req.get("timeout", 30), 30)
USER_AGENT = _as_str(
    _req.get(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    )
)

_ver = _cfg.get("verification", {}) if isinstance(_cfg, dict) else {}
VERIFICATION_CODE_TIMEOUT = _as_int(_ver.get("timeout", 60), 60)
VERIFICATION_CODE_INTERVAL = _as_int(_ver.get("interval", 3), 3)
VERIFICATION_CODE_MAX_RETRIES = _as_int(_ver.get("max_retries", 20), 20)


# -------------------- Browser --------------------
_browser = _cfg.get("browser", {}) if isinstance(_cfg, dict) else {}
BROWSER_WAIT_TIMEOUT = _as_int(_browser.get("wait_timeout", 60), 60)
BROWSER_SHORT_WAIT = _as_int(_browser.get("short_wait", 10), 10)
BROWSER_HEADLESS = _as_bool(_browser.get("headless", False), False)


# -------------------- Email provider --------------------
# Supported providers: gptmail, duckmail
_email = _cfg.get("email", {}) if isinstance(_cfg, dict) else {}
EMAIL_PROVIDER = (
    _as_str(os.getenv("EMAIL_PROVIDER"), "").strip().lower()
    or _as_str(_email.get("provider", ""), "").strip().lower()
)
if not EMAIL_PROVIDER:
    # If user sets duckmail_apikey only, auto-switch to duckmail.
    duckmail_key_hint = (
        _as_str(os.getenv("duckmail_apikey"), "").strip()
        or _as_str(os.getenv("DUCKMAIL_APIKEY"), "").strip()
        or _as_str(os.getenv("DUCKMAIL_API_KEY"), "").strip()
        or _as_str((_cfg.get("duckmail", {}) or {}).get("api_key"), "").strip()
    )
    if duckmail_key_hint:
        EMAIL_PROVIDER = "duckmail"
    else:
        EMAIL_PROVIDER = "gptmail"
if EMAIL_PROVIDER not in ("gptmail", "duckmail"):
    _log_config("WARNING", "email", f"未知邮箱服务: {EMAIL_PROVIDER}, 已回退到 gptmail")
    EMAIL_PROVIDER = "gptmail"

# GPTMail (supports env override for Docker deployments)
_gptmail = _cfg.get("gptmail", {}) if isinstance(_cfg, dict) else {}
GPTMAIL_API_BASE = _as_str(_gptmail.get("api_base", "https://mail.chatgpt.org.uk"), "https://mail.chatgpt.org.uk")
GPTMAIL_API_KEY = _as_str(os.getenv("GPTMAIL_API_KEY"), "") or _as_str(_gptmail.get("api_key", ""), "")
GPTMAIL_PREFIX = _as_str(_gptmail.get("prefix", ""), "")
GPTMAIL_DOMAINS = _gptmail.get("domains", []) if isinstance(_gptmail.get("domains", []), list) else []


def get_random_gptmail_domain() -> str:
    if isinstance(GPTMAIL_DOMAINS, list) and GPTMAIL_DOMAINS:
        return random.choice(GPTMAIL_DOMAINS)
    return ""


# DuckMail (supports env override for Docker deployments)
_duckmail = _cfg.get("duckmail", {}) if isinstance(_cfg, dict) else {}
DUCKMAIL_API_BASE = (
    _as_str(os.getenv("DUCKMAIL_API_BASE"), "").strip().rstrip("/")
    or _as_str(_duckmail.get("api_base", "https://api.duckmail.sbs"), "https://api.duckmail.sbs").strip().rstrip("/")
)
# Primary requested env var name: duckmail_apikey (lowercase)
DUCKMAIL_API_KEY = (
    _as_str(os.getenv("duckmail_apikey"), "").strip()
    or _as_str(os.getenv("DUCKMAIL_APIKEY"), "").strip()
    or _as_str(os.getenv("DUCKMAIL_API_KEY"), "").strip()
    or _as_str(_duckmail.get("api_key", ""), "").strip()
)
DUCKMAIL_PREFIX = _as_str(_duckmail.get("prefix", ""), "")
DUCKMAIL_DOMAINS = _duckmail.get("domains", []) if isinstance(_duckmail.get("domains", []), list) else []


def get_random_duckmail_domain() -> str:
    if isinstance(DUCKMAIL_DOMAINS, list) and DUCKMAIL_DOMAINS:
        return random.choice(DUCKMAIL_DOMAINS)
    return ""


# -------------------- LongCat --------------------
_longcat = _cfg.get("longcat", {}) if isinstance(_cfg, dict) else {}
LONGCAT_PASSPORT_LOGIN_URL = _as_str(_longcat.get("passport_login_url", ""), "").strip()
LONGCAT_KEYS_COUNT = _as_int(_longcat.get("keys_count", 1), 1)
LONGCAT_KEYS_FILE = _as_str(_longcat.get("keys_file", "temp/longcat_keys.txt"), "temp/longcat_keys.txt").strip()
LONGCAT_CSV_PATH = _as_str(_longcat.get("csv_path", "temp/longcat_keys.csv"), "temp/longcat_keys.csv").strip()

# LongCat quota apply (UI automation only; best-effort)
LONGCAT_APPLY_QUOTA = _as_bool(_longcat.get("apply_quota", True), True)
LONGCAT_QUOTA_INDUSTRY = _as_str(_longcat.get("quota_industry", "Internet"), "Internet").strip()
LONGCAT_QUOTA_SCENARIO = _as_str(_longcat.get("quota_scenario", "Chatbot"), "Chatbot").strip()


# -------------------- GPT-Load --------------------
_gpt_load = _cfg.get("gpt_load", {}) if isinstance(_cfg, dict) else {}
# Default to enabled (best-effort). If auth_key is missing, the caller will skip syncing.
GPT_LOAD_SYNC_ENABLED = _as_bool(_gpt_load.get("enabled", True), True)
GPT_LOAD_BASE_URL = (
    _as_str(os.getenv("GPT_LOAD_BASE_URL"), "").strip()
    or _as_str(_gpt_load.get("base_url", "https://great429gptload.zeabur.app"), "https://great429gptload.zeabur.app").strip()
)
GPT_LOAD_GROUP_NAME = (
    _as_str(os.getenv("GPT_LOAD_GROUP_NAME"), "").strip()
    or _as_str(_gpt_load.get("group_name", "#pinhaofan"), "#pinhaofan").strip()
)
GPT_LOAD_AUTH_KEY = (
    _as_str(os.getenv("GPT_LOAD_AUTH_KEY"), "").strip()
    or _as_str(_gpt_load.get("auth_key", ""), "").strip()
)
GPT_LOAD_FORCE = _as_bool(_gpt_load.get("force", False), False)
GPT_LOAD_POLL = _as_bool(_gpt_load.get("poll", True), True)
try:
    GPT_LOAD_POLL_TIMEOUT_S = float(_gpt_load.get("poll_timeout_s", 120.0))
except Exception:
    GPT_LOAD_POLL_TIMEOUT_S = 120.0
try:
    GPT_LOAD_POLL_INTERVAL_S = float(_gpt_load.get("poll_interval_s", 1.0))
except Exception:
    GPT_LOAD_POLL_INTERVAL_S = 1.0
GPT_LOAD_STATE_FILE = _as_str(_gpt_load.get("state_file", ""), "").strip()


#
# Note: A protocol-only POC previously existed under temp/, but the supported flow
# is browser-based, so we keep the config surface minimal here.
