"""
Minimal browser helpers used by the LongCat automation.

This repo originally contained much larger OpenAI/CRS/CPA browser flows.
Those are intentionally removed to keep the project focused on:
  - MyKeeta passport email OTP login (LongCat)
  - LongCat API key creation
  - Optional GPT-Load sync
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Optional

from DrissionPage import ChromiumOptions, ChromiumPage

from config import (
    BROWSER_HEADLESS,
)
from logger import log


BROWSER_MAX_RETRIES = 3
BROWSER_RETRY_DELAY_S = 2
PAGE_LOAD_TIMEOUT_S = 15


def cleanup_chrome_processes() -> None:
    """Best-effort cleanup of Chrome/Chromedriver leftovers (Windows-only)."""
    try:
        if os.name != "nt":
            return
        # Only attempt to kill known automation leftovers.
        subprocess.run(["taskkill", "/F", "/IM", "chromedriver.exe"], capture_output=True, timeout=5)
    except Exception:
        pass


def init_browser(max_retries: int = BROWSER_MAX_RETRIES) -> ChromiumPage:
    log.info("初始化浏览器...", icon="browser")

    last_error = None
    for attempt in range(max(1, int(max_retries or 1))):
        try:
            if attempt > 0:
                log.warning(f"浏览器启动重试 ({attempt + 1}/{max_retries})...")
                cleanup_chrome_processes()
                time.sleep(BROWSER_RETRY_DELAY_S)

            co = ChromiumOptions()
            co.set_argument("--no-first-run")
            co.set_argument("--disable-infobars")
            co.set_argument("--incognito")
            co.set_argument("--disable-gpu")
            co.set_argument("--disable-dev-shm-usage")
            co.set_argument("--no-sandbox")
            co.auto_port()

            if BROWSER_HEADLESS:
                co.set_argument("--headless=new")
                co.set_argument("--window-size=1920,1080")
                log.step("启动 Chrome (无头模式)...")
            else:
                log.step("启动 Chrome (无痕模式)...")

            # Avoid inheriting system proxies implicitly.
            # If you need proxies, set them at the OS / container level.
            co.set_argument("--no-proxy-server")

            co.set_timeouts(base=PAGE_LOAD_TIMEOUT_S, page_load=PAGE_LOAD_TIMEOUT_S * 2)

            page = ChromiumPage(co)
            log.success("浏览器启动成功")
            return page
        except Exception as e:
            last_error = e
            log.warning(f"浏览器启动失败 (尝试 {attempt + 1}/{max_retries}): {e}")
            cleanup_chrome_processes()

    log.error(f"浏览器启动失败，已重试 {max_retries} 次: {last_error}")
    raise last_error  # type: ignore[misc]


def wait_for_page_stable(page, timeout: int = 10, check_interval: float = 0.5) -> bool:
    """Wait for document.readyState == complete and HTML length stops changing."""
    start_time = time.time()
    last_html_len = 0
    stable_count = 0

    while time.time() - start_time < timeout:
        try:
            ready_state = page.run_js("return document.readyState", timeout=2)
            if ready_state != "complete":
                stable_count = 0
                time.sleep(check_interval)
                continue

            cur_len = len(page.html or "")
            if cur_len == last_html_len:
                stable_count += 1
                if stable_count >= 3:
                    return True
            else:
                stable_count = 0
                last_html_len = cur_len
            time.sleep(check_interval)
        except Exception:
            time.sleep(check_interval)
    return False


def wait_for_element(page, selector: str, timeout: int = 10, visible: bool = True):
    """Poll element lookup to reduce flakiness on SPA pages."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            el = page.ele(selector, timeout=1)
            if el:
                if not visible or (getattr(el, "states", None) and el.states.is_displayed) or not hasattr(el, "states"):
                    return el
        except Exception:
            pass
        time.sleep(0.3)
    return None
