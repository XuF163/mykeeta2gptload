# ==================== LongCat (MyKeeta Passport) Automation ====================
# Email-based signup/login via https://passport.mykeeta.com and API key creation
# on https://longcat.chat/platform/api_keys.
#
# Notes:
# - We intentionally run the API key creation request inside the authenticated
#   browser session (page.run_js + fetch) to avoid fragile cookie exporting.
# - Some verification emails use 4-digit OTPs (see email_service.py patterns).
#
# This module is designed to fit the project's existing automation style:
# DrissionPage for browser automation + project logger.

from __future__ import annotations

import json
import os
import random
import string
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from logger import log
from browser_utils import (
    init_browser,
    wait_for_page_stable,
    wait_for_element,
)
from email_service import unified_create_email, unified_get_verification_code
from config import VERIFICATION_CODE_INTERVAL, VERIFICATION_CODE_MAX_RETRIES
from config import LONGCAT_APPLY_QUOTA, LONGCAT_QUOTA_INDUSTRY, LONGCAT_QUOTA_SCENARIO


DEFAULT_PASSPORT_LOGIN_URL = (
    "https://passport.mykeeta.com/pc/login?"
    "locale=en&region=HK&joinkey=1101498_851697727&token_id=5oTEq210UBLUcm4tcuuy6A"
    "&service=consumer&risk_cost_id=119801&theme=longcat&cityId=810001&backurl="
    "https%3A%2F%2Flongcat.chat%2Fapi%2Fv1%2Fuser-loginV3%3Furl%3Dhttps%253A%252F%252Flongcat.chat%252Fplatform%252Fprofile"
)

DEFAULT_SAVE_PATH = None  # prefer CSV for persistence; JSONL can be enabled explicitly
DEFAULT_CSV_PATH = "temp/longcat_keys.csv"
DEFAULT_MAX_ATTEMPTS = 2


def _is_displayed(el) -> bool:
    try:
        return bool(el.states.is_displayed) if hasattr(el, "states") else True
    except Exception:
        return True


def _safe_attr(el, name: str) -> str:
    try:
        v = el.attr(name)
        return "" if v is None else str(v)
    except Exception:
        return ""


def _pick_email_input(page):
    """Pick the best candidate input for an email address."""
    candidates = []
    try:
        candidates = page.eles("css:input", timeout=2) or []
    except Exception:
        candidates = []

    best = None
    best_score = -10_000
    for el in candidates:
        if not el or not _is_displayed(el):
            continue
        t = _safe_attr(el, "type").lower()
        ph = (_safe_attr(el, "placeholder") or "").lower()
        aria = (_safe_attr(el, "aria-label") or "").lower()
        name = (_safe_attr(el, "name") or "").lower()

        score = 0
        if t == "email":
            score += 100
        if "email" in ph or "email" in aria or "email" in name:
            score += 80
        if t in ("text", "email"):
            score += 10
        # Penalize likely OTP inputs.
        if _safe_attr(el, "maxlength") == "1":
            score -= 200
        if "code" in ph or "verification" in ph:
            score -= 50

        if score > best_score:
            best = el
            best_score = score

    return best


def _pick_otp_inputs(page, expected_len: int) -> list:
    """Pick OTP digit inputs. Usually 4 inputs on this flow."""
    inputs = []
    try:
        inputs = page.eles("css:input", timeout=3) or []
    except Exception:
        inputs = []

    # Fast path: Passport overseas OTP inputs use a stable class name.
    try:
        cls_inputs = page.eles("css:input.oversea-verification-code-input", timeout=1) or []
        cls_inputs = [el for el in cls_inputs if el and _is_displayed(el)]
        # Sort left-to-right.
        cls_inputs.sort(key=lambda el: (getattr(el.rect, "location", (10_000, 10_000))[1], getattr(el.rect, "location", (10_000, 10_000))[0]) if hasattr(el, "rect") else (10_000, 10_000))
        if len(cls_inputs) >= expected_len:
            return cls_inputs[:expected_len]
    except Exception:
        pass

    def _dedupe(els: list) -> list:
        seen = set()
        out = []
        for el in els:
            if not el:
                continue
            key = None
            for attr in ("_backend_id", "_node_id", "_obj_id"):
                if hasattr(el, attr):
                    try:
                        key = (attr, getattr(el, attr))
                        break
                    except Exception:
                        pass
            if key is None:
                key = ("pyid", id(el))
            if key in seen:
                continue
            seen.add(key)
            out.append(el)
        return out

    def _pos(el):
        try:
            x, y = el.rect.location
            return (y, x)
        except Exception:
            return (10_000, 10_000)

    def _rect(el):
        try:
            x, y = el.rect.location
            w, h = el.rect.size
            return x, y, w, h
        except Exception:
            return None

    # 0) Geometry-first: on this page the OTP boxes are 4 small square-ish inputs in a row.
    # This is more robust than relying on maxlength/inputmode attributes (which are not always set).
    geo = []
    for el in inputs:
        if not el or not _is_displayed(el):
            continue
        t = _safe_attr(el, "type").lower()
        if t in ("email", "password"):
            continue
        r = _rect(el)
        if not r:
            continue
        x, y, w, h = r
        if w <= 0 or h <= 0:
            continue
        if w < 26 or w > 140 or h < 26 or h > 140:
            continue
        ratio = w / float(h) if h else 0
        if ratio < 0.55 or ratio > 1.9:
            continue
        # Avoid picking random inputs that already contain long text.
        try:
            v = _read_input_value(el) or ""
            if len(v) > 2:
                continue
        except Exception:
            pass
        geo.append((y, x, w, h, el))

    if geo:
        # Cluster by Y (row). Inputs in the same row typically share very similar top coords.
        geo.sort(key=lambda t: (t[0], t[1]))
        clusters = []  # list[dict(y=float, items=list[tuple])]
        for item in geo:
            placed = False
            for c in clusters:
                if abs(item[0] - c["y"]) <= 18:
                    c["items"].append(item)
                    # Update representative y (simple average) to keep clustering stable.
                    c["y"] = sum(it[0] for it in c["items"]) / float(len(c["items"]))
                    placed = True
                    break
            if not placed:
                clusters.append({"y": float(item[0]), "items": [item]})

        best = None
        best_score = -10_000
        for c in clusters:
            items = c["items"]
            if len(items) < expected_len:
                continue
            items = sorted(items, key=lambda t: t[1])  # by x
            # Prefer rows with many candidates and closer to the top.
            score = len(items) * 1000 - int(c["y"])
            if score > best_score:
                best = items
                best_score = score

        if best:
            best = _dedupe([it[4] for it in best])
            if len(best) >= expected_len:
                return best[:expected_len]

    # 1) Prefer the real OTP boxes: visible inputs with maxlength=1.
    otp = []
    for el in inputs:
        if not el or not _is_displayed(el):
            continue
        if _safe_attr(el, "maxlength") == "1":
            otp.append(el)

    otp = _dedupe(otp)
    otp.sort(key=_pos)
    if len(otp) >= expected_len:
        return otp[:expected_len]

    # 2) Fallback: include other numeric-like inputs (some widgets use type=tel).
    otp2 = []
    for el in inputs:
        if not el or not _is_displayed(el):
            continue
        maxlength = _safe_attr(el, "maxlength")
        inputmode = _safe_attr(el, "inputmode").lower()
        t = _safe_attr(el, "type").lower()
        if maxlength == "1" or inputmode in ("numeric", "tel") or t in ("tel", "number"):
            otp2.append(el)

    otp2 = _dedupe(otp2)
    otp2.sort(key=_pos)
    if len(otp2) >= expected_len:
        return otp2[:expected_len]

    # 3) Last resort: take the first N visible inputs in DOM order, sorted by position.
    visible = [el for el in inputs if el and _is_displayed(el)]
    visible = _dedupe(visible)
    visible.sort(key=_pos)
    return visible[:expected_len]


def _read_input_value(el) -> str:
    """Read an input's current value reliably."""
    try:
        v = el.run_js("return this.value")
        return "" if v is None else str(v)
    except Exception:
        try:
            v = el.attr("value")
            return "" if v is None else str(v)
        except Exception:
            return ""


def _otp_values_via_js(page, n: int) -> str:
    """Read OTP values in visual order (more reliable than element.attr).

    The Passport OTP widget can re-render inputs while typing. Querying the DOM
    (filtered to visible enabled inputs) tends to be more stable than holding
    Python element references.
    """
    try:
        js = f"""
            try {{
              const isVisible = (el) => {{
                if (!el) return false;
                if (el.type === 'hidden') return false;
                if (el.disabled) return false;
                const st = window.getComputedStyle(el);
                if (!st) return false;
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                const op = parseFloat(st.opacity || '1');
                if (!Number.isNaN(op) && op <= 0.01) return false;
                const r = el.getBoundingClientRect();
                return r && r.width > 0 && r.height > 0;
              }};

              const pickOtp = (count) => {{
                const all = Array.from(document.querySelectorAll('input')).filter(isVisible);
                const cands = all
                  .map(el => {{ const r = el.getBoundingClientRect(); return {{ el, r }}; }})
                  .filter(x => x.r.width >= 26 && x.r.width <= 140 && x.r.height >= 26 && x.r.height <= 140)
                  .filter(x => {{ const ratio = x.r.width / (x.r.height || 1); return ratio >= 0.55 && ratio <= 1.9; }});

                cands.sort((a, b) => (a.r.top - b.r.top) || (a.r.left - b.r.left));

                const clusters = [];
                for (const it of cands) {{
                  let placed = false;
                  for (const c of clusters) {{
                    if (Math.abs(it.r.top - c.y) <= 18) {{
                      c.items.push(it);
                      c.y = c.items.reduce((s, t) => s + t.r.top, 0) / c.items.length;
                      placed = true;
                      break;
                    }}
                  }}
                  if (!placed) clusters.push({{ y: it.r.top, items: [it] }});
                }}

                let best = null;
                let bestScore = -1e9;
                for (const c of clusters) {{
                  if (c.items.length < count) continue;
                  const score = c.items.length * 1000 - c.y;
                  if (score > bestScore) {{ best = c; bestScore = score; }}
                }}
                if (!best) return [];
                best.items.sort((a, b) => a.r.left - b.r.left);
                return best.items.slice(0, count).map(x => x.el);
              }};

              const els = pickOtp({int(n)});
              return els.map(e => (e.value || '').slice(0, 1)).join('');
            }} catch (e) {{
              return '';
            }}
        """
        v = page.run_js(js, timeout=3)
        return "" if v is None else str(v)
    except Exception:
        return ""


def _set_otp_via_js(page, code: str) -> bool:
    """Set OTP digit inputs via JS (avoids focus jitter during typing)."""
    digits = [c for c in str(code).strip() if c.isdigit()]
    if not digits:
        return False
    try:
        code_json = json.dumps("".join(digits))
        js = f"""
            try {{
              const code = {code_json};
              const n = code.length;
              const nativeSet = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;

              const isVisible = (el) => {{
                if (!el) return false;
                if (el.type === 'hidden') return false;
                if (el.disabled) return false;
                const st = window.getComputedStyle(el);
                if (!st) return false;
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                const op = parseFloat(st.opacity || '1');
                if (!Number.isNaN(op) && op <= 0.01) return false;
                const r = el.getBoundingClientRect();
                return r && r.width > 0 && r.height > 0;
              }};

              const pickOtp = (count) => {{
                const all = Array.from(document.querySelectorAll('input')).filter(isVisible);
                const cands = all
                  .map(el => {{ const r = el.getBoundingClientRect(); return {{ el, r }}; }})
                  .filter(x => x.r.width >= 26 && x.r.width <= 140 && x.r.height >= 26 && x.r.height <= 140)
                  .filter(x => {{ const ratio = x.r.width / (x.r.height || 1); return ratio >= 0.55 && ratio <= 1.9; }});

                cands.sort((a, b) => (a.r.top - b.r.top) || (a.r.left - b.r.left));

                const clusters = [];
                for (const it of cands) {{
                  let placed = false;
                  for (const c of clusters) {{
                    if (Math.abs(it.r.top - c.y) <= 18) {{
                      c.items.push(it);
                      c.y = c.items.reduce((s, t) => s + t.r.top, 0) / c.items.length;
                      placed = true;
                      break;
                    }}
                  }}
                  if (!placed) clusters.push({{ y: it.r.top, items: [it] }});
                }}

                let best = null;
                let bestScore = -1e9;
                for (const c of clusters) {{
                  if (c.items.length < count) continue;
                  const score = c.items.length * 1000 - c.y;
                  if (score > bestScore) {{ best = c; bestScore = score; }}
                }}
                if (!best) return [];
                best.items.sort((a, b) => a.r.left - b.r.left);
                return best.items.slice(0, count).map(x => x.el);
              }};

              const target = pickOtp(n);
              if (target.length < n) return false;

              // Clear first (helps if the widget keeps previous attempt values).
              for (const el of target) {{
                try {{
                  if (nativeSet) nativeSet.call(el, '');
                  else el.value = '';
                  el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                  el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }} catch (e) {{}}
              }}

              for (let i = 0; i < target.length; i++) {{
                const el = target[i];
                const ch = String(code[i] || '').slice(0, 1);
                try {{
                  el.focus();
                  if (nativeSet) nativeSet.call(el, ch);
                  else el.value = ch;
                  el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                  el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }} catch (e) {{}}
              }}
              return true;
            }} catch (e) {{
              return false;
            }}
        """
        ok = page.run_js(js, timeout=4)
        return bool(ok)
    except Exception:
        return False


def _otp_submit_button_enabled_via_js(page) -> bool:
    """Best-effort check whether the OTP submit/continue button is enabled."""
    try:
        js = """
            try {
              const isVisible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (!st) return false;
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                const r = el.getBoundingClientRect();
                return r && r.width > 0 && r.height > 0;
              };
              const btn =
                document.querySelector('.submit-btn') ||
                document.querySelector('button[type="submit"]') ||
                Array.from(document.querySelectorAll('button')).find(b => /continue/i.test((b.innerText || '').trim()));
              if (!btn || !isVisible(btn)) return false;
              const ariaDisabled = (btn.getAttribute('aria-disabled') || '').toLowerCase();
              if (ariaDisabled === 'true') return false;
              return !btn.disabled;
            } catch (e) {
              return false;
            }
        """
        v = page.run_js(js, timeout=2)
        return bool(v)
    except Exception:
        return False


def _find_otp_submit_button(page):
    btn = wait_for_element(page, "css:.submit-btn", timeout=2)
    if not btn:
        btn = wait_for_element(page, "css:button[type=\"submit\"]", timeout=2)
    if not btn:
        btn = wait_for_element(page, "text:Continue", timeout=2)
    return btn


def _is_element_enabled(el) -> bool:
    """Heuristic enabled check for both <button> and styled div/button containers."""
    if not el:
        return False
    try:
        # Common patterns: disabled attribute, aria-disabled, disabled CSS class.
        aria = (_safe_attr(el, "aria-disabled") or "").strip().lower()
        if aria == "true":
            return False
        disabled_attr = (_safe_attr(el, "disabled") or "").strip().lower()
        if disabled_attr in ("disabled", "true"):
            return False
        cls = (_safe_attr(el, "class") or "").lower()
        if "disabled" in cls or "is-disabled" in cls:
            return False
    except Exception:
        pass

    try:
        # Prefer DOM property checks when possible.
        v = el.run_js(
            """
            try {
              const aria = (this.getAttribute('aria-disabled') || '').toLowerCase();
              const cls = (this.className || '').toLowerCase();
              const pe = (window.getComputedStyle(this).pointerEvents || '');
              const d = !!this.disabled;
              if (aria === 'true') return false;
              if (d) return false;
              if (cls.includes('disabled') || cls.includes('is-disabled')) return false;
              if (pe === 'none') return false;
              return true;
            } catch (e) {
              return true;
            }
            """
        )
        return bool(v)
    except Exception:
        return True


def _debug_dump_otp(page) -> None:
    """Dump OTP DOM info when LONGCAT_DEBUG_OTP=1 (helps diagnose focus/rerender issues)."""
    if not os.getenv("LONGCAT_DEBUG_OTP"):
        return
    try:
        js = """
            try {
              const isVisible = (el) => {
                if (!el) return false;
                if (el.type === 'hidden') return false;
                const st = window.getComputedStyle(el);
                if (!st) return false;
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                const r = el.getBoundingClientRect();
                return r && r.width > 0 && r.height > 0;
              };

              const iframeSrcs = Array.from(document.querySelectorAll('iframe')).map(f => f.src || '');
              const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible);

              const cands = inputs
                .map(el => { const r = el.getBoundingClientRect(); return { el, r }; })
                .filter(x => x.r.width >= 26 && x.r.width <= 140 && x.r.height >= 26 && x.r.height <= 140)
                .filter(x => { const ratio = x.r.width / (x.r.height || 1); return ratio >= 0.55 && ratio <= 1.9; });

              cands.sort((a, b) => (a.r.top - b.r.top) || (a.r.left - b.r.left));

              const clusters = [];
              for (const it of cands) {
                let placed = false;
                for (const c of clusters) {
                  if (Math.abs(it.r.top - c.y) <= 18) {
                    c.items.push(it);
                    c.y = c.items.reduce((s, t) => s + t.r.top, 0) / c.items.length;
                    placed = true;
                    break;
                  }
                }
                if (!placed) clusters.push({ y: it.r.top, items: [it] });
              }

              let best = null;
              let bestScore = -1e9;
              for (const c of clusters) {
                if (c.items.length < 2) continue;
                const score = c.items.length * 1000 - c.y;
                if (score > bestScore) { best = c; bestScore = score; }
              }

              const bestEls = best ? best.items.sort((a, b) => a.r.left - b.r.left).slice(0, 8) : [];

              return {
                iframeCount: iframeSrcs.length,
                iframeSrcs: iframeSrcs.slice(0, 3),
                inputCount: inputs.length,
                otpLikeInputs: bestEls.map(x => ({
                  value: x.el.value || '',
                  disabled: !!x.el.disabled,
                  top: Math.round(x.r.top),
                  left: Math.round(x.r.left),
                  width: Math.round(x.r.width),
                  height: Math.round(x.r.height),
                  type: x.el.getAttribute('type') || '',
                  name: x.el.getAttribute('name') || '',
                  id: x.el.id || '',
                  className: x.el.className || '',
                  inputmode: x.el.getAttribute('inputmode') || '',
                  autocomplete: x.el.getAttribute('autocomplete') || '',
                }))
              };
            } catch (e) {
              return { error: String(e || '') };
            }
        """
        data = page.run_js(js, timeout=3) or []
        log.info(f"OTP debug inputs: {json.dumps(data)[:1200]}")
    except Exception as e:
        log.warning(f"OTP debug dump failed: {e}")

    # Python-side dump (works even when JS querySelector can't see the inputs, e.g. shadow/iframe).
    try:
        els = []
        try:
            els = page.eles("css:input", timeout=1) or []
        except Exception:
            els = []
        rows = []
        for el in els[:30]:
            try:
                rows.append(
                    {
                        "type": (_safe_attr(el, "type") or ""),
                        "maxlength": (_safe_attr(el, "maxlength") or ""),
                        "inputmode": (_safe_attr(el, "inputmode") or ""),
                        "name": (_safe_attr(el, "name") or ""),
                        "placeholder": (_safe_attr(el, "placeholder") or ""),
                        "aria": (_safe_attr(el, "aria-label") or ""),
                        "value": _read_input_value(el)[:4],
                        "displayed": _is_displayed(el),
                    }
                )
            except Exception:
                pass
        log.info(f"OTP debug python inputs (first {len(rows)}): {json.dumps(rows)[:1200]}")
    except Exception as e:
        log.warning(f"OTP debug python dump failed: {e}")


def _fill_otp(page, code: str) -> bool:
    """Fill OTP and return True if the page is likely ready to submit.

    The Passport OTP widget is unstable (focus stealing + re-rendering). We try
    JS/element-level input first and only do relaxed client-side validation here.
    The real verification is server-side after clicking submit.
    """
    digits = [c for c in str(code).strip() if c.isdigit()]
    if not digits:
        return False

    _debug_dump_otp(page)

    expected = "".join(digits)

    def _continue_enabled() -> bool:
        btn = _find_otp_submit_button(page)
        if btn and _is_element_enabled(btn):
            return True
        return _otp_submit_button_enabled_via_js(page)

    # Give the OTP widget a moment to finish its transitions (prevents focus-jitter).
    time.sleep(0.5)

    # Up to a few attempts because the widget sometimes re-renders inputs mid-fill.
    for _attempt in range(6):
        otp_inputs = _pick_otp_inputs(page, expected_len=len(digits))

        # Strategy 0: JS set values + dispatch events (best when we can locate inputs in the DOM).
        _set_otp_via_js(page, expected)
        time.sleep(0.15)
        if _continue_enabled():
            return True

        if otp_inputs and len(otp_inputs) >= len(digits):
            # Clear all boxes first (avoid mixing previous attempt digits).
            for el in otp_inputs[: len(digits)]:
                try:
                    el.input("", clear=True)
                except Exception:
                    try:
                        el.run_js(
                            """
                            try {
                              const nativeSet = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                              if (nativeSet) nativeSet.call(this, '');
                              else this.value = '';
                              this.dispatchEvent(new Event('input', {bubbles:true}));
                              this.dispatchEvent(new Event('change', {bubbles:true}));
                            } catch (e) {}
                            """
                        )
                    except Exception:
                        pass

            # Focus the first (left-most) box and type slowly; widget should auto-advance.
            try:
                otp_inputs[0].click()
            except Exception:
                try:
                    otp_inputs[0].run_js("this.focus()")
                except Exception:
                    pass

            for ch in expected:
                try:
                    page.actions.type(str(ch))
                except Exception:
                    pass
                time.sleep(0.28)

            time.sleep(0.25)
            if _continue_enabled():
                return True

            # Fallback: per-element input (targets each box explicitly).
            for i, ch in enumerate(digits):
                otp_inputs = _pick_otp_inputs(page, expected_len=len(digits))
                if not otp_inputs or len(otp_inputs) < len(digits):
                    break
                try:
                    otp_inputs[i].input(str(ch), clear=True)
                except Exception:
                    try:
                        otp_inputs[i].input(str(ch))
                    except Exception:
                        pass
                time.sleep(0.12)

            time.sleep(0.25)
            if _continue_enabled():
                return True
        else:
            # Last resort: just type into whatever is focused.
            try:
                for ch in expected:
                    page.actions.type(str(ch))
                    time.sleep(0.35)
            except Exception:
                pass
            if _continue_enabled():
                return True

        time.sleep(0.35)

    _debug_dump_otp(page)
    return False


def _wait_url_contains(page, needle: str, timeout: int = 30) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            if needle in (page.url or ""):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _longcat_user_current(page, timeout_s: int = 12) -> Optional[dict]:
    """Fetch LongCat user info to verify the session is authenticated."""
    try:
        # Ensure we are on longcat origin; cross-origin fetch from passport page may be blocked.
        if "longcat.chat" not in (page.url or ""):
            page.get("https://longcat.chat/")
            wait_for_page_stable(page, timeout=8)
    except Exception:
        pass

    js = f"""
        return Promise.race([
            fetch('https://longcat.chat/api/v1/user-current', {{
                method: 'GET',
                credentials: 'include'
            }})
            .then(r => r.text())
            .catch(() => ''),
            new Promise((_, reject) => setTimeout(() => reject('timeout'), {timeout_s * 1000}))
        ]).catch(() => '');
    """
    raw = page.run_js(js, timeout=timeout_s + 4)
    if not raw or raw == "timeout":
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _is_longcat_authenticated(page) -> bool:
    data = _longcat_user_current(page)
    if not isinstance(data, dict):
        return False
    if data.get("code") != 0:
        return False
    payload = data.get("data")
    return bool(payload)


def _extract_backurl(passport_login_url: str) -> Optional[str]:
    """Extract and decode the `backurl` param from the passport URL (if present)."""
    try:
        qs = parse_qs(urlparse(passport_login_url).query)
        raw = qs.get("backurl", [None])[0]
        if not raw:
            return None
        # backurl is URL-encoded; decode once.
        return unquote(raw)
    except Exception:
        return None


def _random_key_name(prefix: str = "key") -> str:
    tail = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{prefix}-{tail}"


def create_longcat_account_and_api_key(
    api_key_name: Optional[str] = None,
    passport_login_url: str = DEFAULT_PASSPORT_LOGIN_URL,
    email: Optional[str] = None,
    gptmail_api_key: Optional[str] = None,
    max_mail_retries: int = VERIFICATION_CODE_MAX_RETRIES,
    mail_interval: int = VERIFICATION_CODE_INTERVAL,
    save_path: str = DEFAULT_SAVE_PATH,
    csv_path: str = DEFAULT_CSV_PATH,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict:
    """Create/login via email OTP, then create an API key on LongCat platform.

    Returns:
        dict: {
            "email": "...",
            "api_key": "ak_...",
            "api_key_name": "...",
            "saved_to": "...",      # JSONL path (optional)
            "saved_csv": "...",     # CSV path (optional)
        }
    """
    api_key_name = api_key_name or _random_key_name("lc")
    backurl = _extract_backurl(passport_login_url)

    max_attempts = int(max_attempts or 1)
    if max_attempts <= 0:
        max_attempts = 1

    last_err = None
    for attempt in range(max_attempts):
        attempt_email = email
        if not attempt_email:
            # Use GPTMail via the unified helper (GPTMail-only in this trimmed repo).
            attempt_email, _pw = unified_create_email()
            if not attempt_email:
                raise RuntimeError("Email creation failed (unified_create_email)")

        log.separator("=", 60)
        log.header("LongCat Email Login + API Key")
        log.separator("=", 60)
        log.info(f"Attempt: {attempt + 1}/{max_attempts}")
        log.info(f"Email: {attempt_email}", icon="email")
        log.info(f"API Key name: {api_key_name}", icon="key")

        page = None
        try:
            page = init_browser()

            log.step("Open passport login URL...")
            page.get(passport_login_url)
            wait_for_page_stable(page, timeout=8)

            # Step 1: choose email login
            log.step("Click 'Continue with email'...")
            btn = wait_for_element(page, "text:Continue with email", timeout=10)
            if not btn:
                # Some locales might render a different casing; fallback to broad search.
                btn = wait_for_element(page, "text:Continue with Email", timeout=3)
            if not btn:
                raise RuntimeError("Cannot find 'Continue with email' button")
            btn.click()
            wait_for_page_stable(page, timeout=5)

            # Step 2: fill email and request OTP
            log.step("Fill email...")
            email_input = _pick_email_input(page)
            if not email_input:
                raise RuntimeError("Cannot find email input")
            email_input.input(attempt_email, clear=True)

            log.step("Click Continue...")
            # Prefer the actual submit container; "Continue" is often just a nested text node.
            cont = wait_for_element(page, "css:.submit-btn", timeout=10)
            if not cont:
                cont = wait_for_element(page, "css:button[type=\"submit\"]", timeout=5)
            if not cont:
                cont = wait_for_element(page, "text:Continue", timeout=5)
            if not cont:
                raise RuntimeError("Cannot find Continue button after email input")
            cont.click()

            # Step 3: wait for OTP screen
            log.step("Wait for OTP inputs...")
            if not wait_for_element(page, "text:Enter Verification Code", timeout=15, visible=True):
                # Some variants might not have the title but still show digit inputs.
                wait_for_page_stable(page, timeout=5)

            # Step 4: poll email for OTP
            log.step("Poll email for OTP...")
            code, err, _email_time = unified_get_verification_code(
                attempt_email, max_retries=max_mail_retries, interval=mail_interval
            )
            if not code:
                raise RuntimeError(f"Failed to get verification code: {err}")
            code = str(code).strip()
            log.success(f"OTP received ({len(code)} digits)")

            # Step 5: fill OTP digits
            if not _fill_otp(page, code):
                raise RuntimeError("OTP fill failed (inputs did not match expected digits)")

            # Submit OTP
            log.step("Submit OTP...")
            cont2 = wait_for_element(page, "css:.submit-btn", timeout=10)
            if not cont2:
                cont2 = wait_for_element(page, "css:button[type=\"submit\"]", timeout=5)
            if not cont2:
                cont2 = wait_for_element(page, "text:Continue", timeout=5)
            if cont2:
                cont2.click()

            # Step 6: wait for redirect to platform
            log.step("Wait for LongCat platform...")
            # Some runs redirect slowly; also occasionally the redirect doesn't auto-navigate.
            # We'll wait a bit, then proactively open the platform to validate the session.
            _wait_url_contains(page, "longcat.chat/platform", timeout=20)
            try:
                # Always visit backurl once to ensure SSO cookie is exchanged on longcat.
                if backurl:
                    page.get(backurl)
                else:
                    page.get("https://longcat.chat/platform/profile")
                wait_for_page_stable(page, timeout=10)
            except Exception:
                pass

            # If we ended up at the Mainland phone-login page, it means the SSO cookie wasn't set.
            # Retry the backurl transfer once more before failing.
            if "longcat.chat/login" in (page.url or "") and backurl:
                try:
                    log.warning("Landed on longcat.chat/login (not authenticated). Retrying backurl transfer...")
                    page.get(backurl)
                    wait_for_page_stable(page, timeout=10)
                except Exception:
                    pass

            if not _wait_url_contains(page, "longcat.chat/platform", timeout=20):
                # We might still be on /login; verify by API.
                if not _is_longcat_authenticated(page):
                    raise RuntimeError(
                        f"Not authenticated on LongCat (SSO failed / fallback login): {page.url}"
                    )

            # Final auth sanity check before creating key.
            if not _is_longcat_authenticated(page):
                raise RuntimeError("Not authenticated on LongCat (user-current check failed)")

            # Step 7: create API key via authenticated fetch (more reliable than UI scraping)
            log.step("Open API Keys page...")
            page.get("https://longcat.chat/platform/api_keys")
            wait_for_page_stable(page, timeout=8)

            log.step("Create API key via fetch...")
            # Use Promise.race for timeout, same style as browser_automation.is_logged_in().
            name_json = json.dumps(api_key_name)
            try:
                loc = page.run_js("return location.href", timeout=2)
                log.info(f"API key page URL: {loc}")
            except Exception:
                log.info(f"API key page URL: {page.url}")

            def _create_key_once() -> str:
                # Return a JSON string with debug fields (status/url/ct/text) so we can
                # distinguish between 401/403/404 and wrong-origin/CORS failures.
                return page.run_js(
                    f"""
                    return Promise.race([
                      (async () => {{
                        try {{
                          const r = await fetch('https://longcat.chat/api/lc-platform/v1/create-apiKeys', {{
                            method: 'POST',
                            credentials: 'include',
                            headers: {{
                              'content-type': 'application/json',
                              'x-requested-with': 'XMLHttpRequest',
                              'x-client-language': 'zh'
                            }},
                            body: JSON.stringify({{name: {name_json}}})
                          }});
                          const ct = r.headers.get('content-type') || '';
                          const text = await r.text();
                          return JSON.stringify({{
                            ok: r.ok,
                            status: r.status,
                            url: r.url,
                            content_type: ct,
                            text: text
                          }});
                        }} catch (e) {{
                          return JSON.stringify({{ok:false,status:0,url:'',content_type:'',text:String(e||'')}});
                        }}
                      }})(),
                      new Promise((_, reject) => setTimeout(() => reject('timeout'), {15 * 1000}))
                    ]).catch(() => 'timeout');
                    """,
                    timeout=20,
                )

            raw = None
            last_dbg = None
            for i in range(4):
                raw = _create_key_once()
                if not raw or raw == "timeout":
                    last_dbg = raw
                else:
                    last_dbg = raw
                    # Try to parse the wrapper JSON first.
                    try:
                        wrap = json.loads(raw)
                    except Exception:
                        wrap = None
                    if isinstance(wrap, dict):
                        text = wrap.get("text") or ""
                        # If the backend occasionally returns an HTML 404, reload and retry.
                        if "<html" in text.lower() or "<!doctype" in text.lower():
                            log.warning(f"API key create returned HTML (attempt {i+1}/4), retrying...")
                            try:
                                page.refresh()
                                wait_for_page_stable(page, timeout=8)
                            except Exception:
                                pass
                            time.sleep(0.6)
                            continue
                        raw = text  # unwrap for normal JSON parsing below
                        break
                time.sleep(0.6)

            if not raw or raw == "timeout":
                raise RuntimeError(f"API key creation request failed or timed out: {last_dbg}")

            try:
                resp = json.loads(raw)
            except Exception as e:
                raise RuntimeError(f"Unexpected API response (not JSON): {raw[:200]}") from e

            if resp.get("code") != 0:
                raise RuntimeError(f"API key creation failed: {resp}")

            api_key = resp.get("data")
            if not api_key or not isinstance(api_key, str):
                raise RuntimeError(f"API key missing in response: {resp}")

            log.success("API key created")
            record = {
                "email": attempt_email,
                "api_key_name": api_key_name,
                "api_key": api_key,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            # Step 8 (optional): Apply for more quota on the "Usage" page.
            quota_applied = None
            quota_applied_at = None
            quota_apply_error = ""
            if bool(LONGCAT_APPLY_QUOTA):
                try:
                    qr = apply_more_quota(page, api_key_name=api_key_name)
                    quota_applied = bool(qr.get("ok"))
                    quota_applied_at = qr.get("applied_at") or None
                    quota_apply_error = str(qr.get("error") or "")
                    if quota_applied:
                        log.success("Quota application submitted")
                    else:
                        log.warning(f"Quota application failed: {quota_apply_error or 'unknown'}")
                except Exception as e:
                    quota_applied = False
                    quota_apply_error = str(e)
                    log.warning(f"Quota application exception: {quota_apply_error}")
            else:
                log.info("Quota application skipped (config.toml [longcat].apply_quota=false)")

            record["quota_applied"] = quota_applied
            record["quota_applied_at"] = quota_applied_at
            record["quota_apply_error"] = quota_apply_error

            saved_to = None
            if save_path:
                try:
                    p = Path(save_path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    with p.open("a", encoding="utf-8") as f:
                        # JSONL append; keep ASCII-only output unless the data forces Unicode.
                        f.write(json.dumps(record, ensure_ascii=True) + "\n")
                    saved_to = str(p)
                    log.success(f"Saved: {saved_to}")
                except Exception as e:
                    log.warning(f"Save failed: {e}")

            saved_csv = None
            if csv_path:
                try:
                    import csv

                    p = Path(csv_path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    header = [
                        "email",
                        "api_key_name",
                        "api_key",
                        "created_at",
                        "quota_applied",
                        "quota_applied_at",
                        "quota_apply_error",
                    ]
                    _ensure_csv_header(p, header)
                    file_exists = p.exists() and p.stat().st_size > 0
                    with p.open("a", encoding="utf-8", newline="") as f:
                        w = csv.writer(f)
                        if not file_exists:
                            w.writerow(header)
                        quota_csv = ""
                        if record.get("quota_applied") is True:
                            quota_csv = "1"
                        elif record.get("quota_applied") is False:
                            quota_csv = "0"
                        if file_exists:
                            actual = _read_csv_header(p) or header
                        else:
                            actual = header

                        if actual == header:
                            w.writerow(
                                [
                                    record["email"],
                                    record["api_key_name"],
                                    record["api_key"],
                                    record["created_at"],
                                    quota_csv,
                                    record.get("quota_applied_at") or "",
                                    record.get("quota_apply_error") or "",
                                ]
                            )
                        else:
                            # Respect the existing schema; fill known columns and leave the rest blank.
                            row_map = {
                                "email": record.get("email") or "",
                                "api_key_name": record.get("api_key_name") or "",
                                "api_key": record.get("api_key") or "",
                                "created_at": record.get("created_at") or "",
                                "quota_applied": quota_csv,
                                "quota_applied_at": record.get("quota_applied_at") or "",
                                "quota_apply_error": record.get("quota_apply_error") or "",
                            }
                            w.writerow([row_map.get(col, "") for col in actual])
                    saved_csv = str(p)
                    log.success(f"Saved CSV: {saved_csv}")
                except Exception as e:
                    log.warning(f"CSV save failed: {e}")

            record["saved_to"] = saved_to
            record["saved_csv"] = saved_csv
            return record
        except Exception as e:
            last_err = e
            log.warning(f"LongCat flow failed on attempt {attempt + 1}/{max_attempts}: {e}")
        finally:
            if page:
                try:
                    page.quit()
                except Exception:
                    pass

        # Retry with a new email unless the caller provided a fixed email.
        if email:
            break

    raise RuntimeError(str(last_err) if last_err else "LongCat flow failed")


def _ensure_csv_header(path: Path, header: list[str]) -> None:
    """Best-effort CSV schema migration (append-only columns).

    We used to write: email, api_key_name, api_key, created_at
    Now we also add quota application columns.
    """
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return

        import csv

        with path.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))

        if not rows:
            return

        existing = list(rows[0] or [])
        if existing and existing[0].startswith("\ufeff"):
            existing[0] = existing[0].lstrip("\ufeff")

        if existing == header:
            return

        old_header = ["email", "api_key_name", "api_key", "created_at"]
        # Only auto-migrate if it's our known old schema (or a strict prefix).
        if existing != old_header and header[: len(existing)] != existing:
            log.warning(f"CSV header mismatch, leaving as-is: {existing}")
            return

        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            for r in rows[1:]:
                out = list(r) + [""] * (len(header) - len(r))
                w.writerow(out[: len(header)])

        os.replace(str(tmp), str(path))
        log.info(f"Upgraded CSV header: {path}")
    except Exception as e:
        log.warning(f"CSV header upgrade failed: {e}")


def _read_csv_header(path: Path) -> Optional[list[str]]:
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return None
        import csv

        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            row = next(reader, None)
        if not row:
            return None
        header = list(row)
        if header and header[0].startswith("\ufeff"):
            header[0] = header[0].lstrip("\ufeff")
        return header
    except Exception:
        return None


def _debug_dump_quota(page, note: str = "") -> None:
    """Dump minimal DOM hints for quota modal/navigation debugging."""
    if not os.getenv("LONGCAT_DEBUG_QUOTA"):
        return
    try:
        url = ""
        try:
            url = page.run_js("return location.href", timeout=2)
        except Exception:
            url = getattr(page, "url", "") or ""
        if note:
            log.info(f"Quota debug ({note}) url: {url}")
        else:
            log.info(f"Quota debug url: {url}")
    except Exception:
        pass

    try:
        js = """
            try {
              const isVisible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (!st) return false;
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                const r = el.getBoundingClientRect();
                return r && r.width > 0 && r.height > 0;
              };
              const buttons = Array.from(document.querySelectorAll('button'))
                .filter(isVisible)
                .map(b => (b.innerText || '').trim())
                .filter(Boolean)
                .slice(0, 25);
              const inputs = Array.from(document.querySelectorAll('input,textarea'))
                .filter(isVisible)
                .filter(el => (el.type || '').toLowerCase() !== 'hidden')
                .map(el => ({
                  tag: el.tagName,
                  type: el.getAttribute('type') || '',
                  placeholder: el.getAttribute('placeholder') || '',
                  value: (el.value || '').slice(0, 32)
                }))
                .slice(0, 25);
              const dialogs = Array.from(document.querySelectorAll('[role="dialog"],.ant-modal-content,.ant-modal,.modal'))
                .filter(isVisible)
                .length;
              return { title: document.title || '', url: location.href, dialogs, buttons, inputs };
            } catch (e) {
              return { error: String(e) };
            }
        """
        data = page.run_js(js, timeout=4)
        log.info(f"Quota debug data: {json.dumps(data, ensure_ascii=True)[:1200]}")
    except Exception as e:
        log.warning(f"Quota debug dump failed: {e}")


def apply_more_quota(
    page,
    api_key_name: Optional[str] = None,
    industry: Optional[str] = None,
    scenario: Optional[str] = None,
) -> dict:
    """Apply for more quota after key creation (best-effort).

    Flow: Usage page -> Apply more quota -> fill Industry/Scenario -> agree -> submit.
    """
    industry = (industry or LONGCAT_QUOTA_INDUSTRY or "Internet").strip()
    scenario = (scenario or LONGCAT_QUOTA_SCENARIO or "Chatbot").strip()

    def _scroll_nudge() -> None:
        """Trigger lazy loads / avoid mobile-layout edge cases (common in small viewports)."""
        try:
            page.run_js(
                """
                try {
                  window.scrollTo(0, 0);
                  setTimeout(() => window.scrollTo(0, document.body.scrollHeight), 150);
                  setTimeout(() => window.scrollTo(0, 0), 300);
                  return true;
                } catch (e) { return false; }
                """,
                timeout=3,
            )
        except Exception:
            pass

    def _install_open_trap() -> None:
        """Capture target=_blank / window.open navigations triggered by the apply button."""
        try:
            page.run_js(
                """
                try {
                  window.__lc_opened_url = '';
                  window.__lc_opened_at = 0;
                  const _origOpen = window.open;
                  window.open = function(url) {
                    try {
                      window.__lc_opened_url = String(url || '');
                      window.__lc_opened_at = Date.now();
                      // Prefer same-tab navigation in automation environments.
                      if (url) location.href = url;
                    } catch (e) {}
                    try { return _origOpen.apply(this, arguments); } catch (e) { return null; }
                  };
                  return true;
                } catch (e) {
                  return false;
                }
                """,
                timeout=3,
            )
        except Exception:
            pass

    def _consume_open_trap() -> str:
        try:
            v = page.run_js("try { return window.__lc_opened_url || ''; } catch (e) { return ''; }", timeout=2)
            return str(v or "").strip()
        except Exception:
            return ""

    def _visible_dialog_count() -> int:
        try:
            js = """
                try {
                  const isVisible = (el) => {
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    if (!st) return false;
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    const r = el.getBoundingClientRect();
                    return r && r.width > 0 && r.height > 0;
                  };
                  const dialogs = Array.from(document.querySelectorAll('[role="dialog"],.ant-modal-content,.ant-modal,.modal')).filter(isVisible);
                  return dialogs.length;
                } catch (e) {
                  return 0;
                }
            """
            v = page.run_js(js, timeout=4)
            return int(v or 0)
        except Exception:
            return 0

    def _quota_form_visible() -> bool:
        """Return True if the 'apply more quota' modal/form is currently visible."""
        try:
            js = """
                try {
                  const isVisible = (el) => {
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    if (!st) return false;
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    const r = el.getBoundingClientRect();
                    return r && r.width > 0 && r.height > 0;
                  };
                  const dialogs = Array.from(document.querySelectorAll('[role="dialog"],.ant-modal-content,.ant-modal,.modal')).filter(isVisible);
                  const dlg = dialogs[0] || null;
                  const scope = dlg || document;

                  // Any visible form-like inputs inside a visible dialog counts as "opened".
                  const hasInputs = Array.from(scope.querySelectorAll('textarea,input,select'))
                    .filter(isVisible)
                    .some(el => ((el.getAttribute && (el.getAttribute('type') || '').toLowerCase()) !== 'hidden'));
                  if (dlg && hasInputs) return true;

                  // Fallback: visible primary/submit button in a dialog.
                  const btn = Array.from(scope.querySelectorAll('button'))
                    .filter(isVisible)
                    .find(b => {
                      const t = (b.innerText || '').trim().toLowerCase();
                      return t.includes('submit') || t.includes('apply') || t.includes('continue') || (b.innerText || '').includes('\\u63d0\\u4ea4');
                    });
                  if (dlg && btn) return true;

                  // Some variants render the quota form as a full page (no dialog).
                  const pageInputs = Array.from(document.querySelectorAll('textarea,input,select'))
                    .filter(isVisible)
                    .filter(el => ((el.getAttribute && (el.getAttribute('type') || '').toLowerCase()) !== 'hidden'));
                  const pageButtons = Array.from(document.querySelectorAll('button'))
                    .filter(isVisible);
                  const looksLikeQuotaForm =
                    pageInputs.length >= 2 &&
                    pageButtons.some(b => {
                      const t = (b.innerText || '').trim().toLowerCase();
                      return t.includes('submit') || (b.innerText || '').includes('\\u63d0\\u4ea4');
                    });
                  return !!looksLikeQuotaForm;
                } catch (e) {
                  return false;
                }
            """
            v = page.run_js(js, timeout=4)
            return bool(v)
        except Exception:
            return False

    _debug_dump_quota(page, note="before_open_usage")

    # 0) Ensure the newly created key is reflected in the UI store.
    if api_key_name:
        try:
            page.get("https://longcat.chat/platform/api_keys")
            wait_for_page_stable(page, timeout=10)
            # The UI is SPA and may cache the key list. Refresh a few times until the
            # new key name appears (or we give up and continue best-effort).
            seen = False
            for _ in range(3):
                if wait_for_element(page, f"text:{api_key_name}", timeout=2):
                    seen = True
                    break
                try:
                    page.refresh()
                except Exception:
                    pass
                wait_for_page_stable(page, timeout=10)
            if not seen:
                log.warning("API key not visible in UI yet (will continue best-effort)")
        except Exception:
            pass

    # 1) Navigate to Usage page (UI text based; routes may change).
    try:
        if "longcat.chat/platform" not in (getattr(page, "url", "") or ""):
            page.get("https://longcat.chat/platform/profile")
            wait_for_page_stable(page, timeout=10)
    except Exception:
        pass

    # Direct URL fallbacks (force full navigation to avoid stale SPA state).
    usage_urls = (
        "https://longcat.chat/platform/usage",
        "https://longcat.chat/platform/usage-info",
        "https://longcat.chat/platform/billing",
        "https://longcat.chat/platform/quota",
    )
    for url in usage_urls:
        try:
            page.get(url)
            wait_for_page_stable(page, timeout=15)
            _scroll_nudge()
            if wait_for_element(page, "text:\u7533\u8bf7\u66f4\u591a\u989d\u5ea6", timeout=2) or wait_for_element(page, "text:Apply", timeout=2):
                break
        except Exception:
            continue

    _debug_dump_quota(page, note="after_open_usage")

    # 2) Click "Apply more quota"
    _scroll_nudge()
    btn = None
    clicked_via_js = False
    btn_text_selectors = [
        "text:\u7533\u8bf7\u66f4\u591a\u989d\u5ea6",  # 申请更多额度
        "text:\u7533\u8bf7\u66f4\u591a\u914d\u989d",  # 申请更多配额
        "text:\u7533\u8bf7\u914d\u989d",              # 申请配额
        "text:\u63d0\u989d",                          # 提额
        "text:Request more quota",
        "text:Apply",
        "text:Quota",
        "text:Increase",
        "text:Request",
    ]
    for sel in btn_text_selectors:
        btn = wait_for_element(page, sel, timeout=4)
        if btn:
            break
    if not btn:
        # Last resort: scan visible clickable elements and click a best match.
        try:
            dialogs_before = _visible_dialog_count()
            _install_open_trap()
            js_click = """
                try {
                  const needles = [
                    'apply', 'quota', 'increase', 'request', 'more',
                    '\\u7533\\u8bf7', '\\u914d\\u989d', '\\u989d\\u5ea6', '\\u63d0\\u989d'
                  ];
                  const isVisible = (el) => {
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    if (!st) return false;
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    const r = el.getBoundingClientRect();
                    return r && r.width > 0 && r.height > 0;
                  };
                  const textOf = (el) => ((el.innerText || el.textContent || '') + '').trim();
                  const clickable = (el) => {
                    const tag = (el.tagName || '').toLowerCase();
                    if (tag === 'button' || tag === 'a') return true;
                    const role = (el.getAttribute && el.getAttribute('role')) || '';
                    if (role === 'button') return true;
                    const cls = (el.className || '') + '';
                    return cls.toLowerCase().includes('btn') || cls.toLowerCase().includes('button');
                  };
                  const els = Array.from(document.querySelectorAll('button,a,[role=\"button\"],div,span'))
                    .filter(isVisible)
                    .filter(clickable);
                  const scored = els.map(el => {
                    const t = textOf(el);
                    const tl = t.toLowerCase();
                    let score = 0;
                    for (const n of needles) if (tl.includes(n)) score += (n.length >= 4 ? 3 : 1);
                    if (tl.includes('apply') && tl.includes('quota')) score += 10;
                    if (t.includes('\\u7533\\u8bf7') && (t.includes('\\u989d\\u5ea6') || t.includes('\\u914d\\u989d'))) score += 10;
                    return { el, t: t.slice(0, 120), score };
                  }).filter(x => x.score > 0).sort((a,b) => b.score - a.score);
                  const best = scored[0] || null;
                  if (!best) return { clicked: false, reason: 'no_match', sample: scored.slice(0,3) };
                  try { best.el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
                  // Click with a fuller mouse sequence to satisfy some UI libs.
                  try { best.el.dispatchEvent(new MouseEvent('mousemove', { bubbles: true })); } catch (e) {}
                  try { best.el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })); } catch (e) {}
                  try { best.el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true })); } catch (e) {}
                  try { best.el.click(); } catch (e) {
                    try { best.el.dispatchEvent(new MouseEvent('click', { bubbles: true })); } catch (e2) {}
                  }
                  return { clicked: true, text: best.t, score: best.score, url: location.href };
                } catch (e) {
                  return { clicked: false, error: String(e), url: location.href };
                }
            """
            r = page.run_js(js_click, timeout=6) or {}
            wait_for_page_stable(page, timeout=6)
            if isinstance(r, dict) and r.get("clicked"):
                _debug_dump_quota(page, note=f"clicked_apply_js:{r.get('text','')[:40]}")
                clicked_via_js = True
            else:
                _debug_dump_quota(page, note="apply_btn_not_found")
                return {
                    "ok": False,
                    "error": f"apply button not found (js_scan={json.dumps(r, ensure_ascii=True)[:400]})",
                    "url": getattr(page, "url", ""),
                }
        except Exception as e:
            _debug_dump_quota(page, note="apply_btn_not_found_exception")
            return {"ok": False, "error": f"apply button not found ({e})", "url": getattr(page, "url", "")}

    # If we clicked via JS, we can't reliably read enabled/disabled state via a page element wrapper.
    # We'll just proceed and detect whether the quota form opened.
    if clicked_via_js:
        wait_for_page_stable(page, timeout=6)
    else:
        _install_open_trap()
        # If the button is disabled, the UI believes there is no API key yet.
        # This can happen when we create the key via API but the SPA store is stale.
        if not _is_element_enabled(btn):
            log.warning("Apply-more-quota button disabled; refreshing to wait for API key sync...")
            for _ in range(3):
                try:
                    page.refresh()
                except Exception:
                    pass
                wait_for_page_stable(page, timeout=10)
                btn = wait_for_element(page, "text:\u7533\u8bf7\u66f4\u591a\u989d\u5ea6", timeout=5) or wait_for_element(
                    page, "text:Apply", timeout=2
                )
                if btn and _is_element_enabled(btn):
                    break
            else:
                return {
                    "ok": False,
                    "error": "apply button disabled (frontend thinks no API key yet)",
                    "url": getattr(page, "url", ""),
                }

        dialogs_before = _visible_dialog_count()
        try:
            try:
                btn.run_js("try { this.scrollIntoView({block:'center',inline:'center'}); } catch(e) {}", timeout=2)
            except Exception:
                pass
            btn.click()
        except Exception:
            try:
                btn.run_js("this.click()")
            except Exception:
                return {"ok": False, "error": "apply button click failed", "url": getattr(page, "url", "")}

    wait_for_page_stable(page, timeout=6)
    _debug_dump_quota(page, note="after_click_apply")

    # The quota form should open in a modal/dialog; if not, we likely clicked the wrong element
    # or the UI requires extra navigation in this environment.
    opened = False
    start = time.time()
    while time.time() - start < 20:
        # If the click triggered a new tab / external navigation, follow it.
        opened_url = _consume_open_trap()
        if opened_url and opened_url.startswith("http"):
            try:
                page.get(opened_url)
                wait_for_page_stable(page, timeout=15)
            except Exception:
                pass
        if _visible_dialog_count() > dialogs_before or _quota_form_visible():
            opened = True
            break
        time.sleep(0.3)

    if not opened:
        # Capture some hints for hosted environments (HF Spaces) without requiring container access.
        dbg: dict = {}
        dbg_err = ""
        dbg_js = """
            try {
              const isVisible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (!st) return false;
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                const r = el.getBoundingClientRect();
                return r && r.width > 0 && r.height > 0;
              };
              const texts = (el) => ((el.innerText || el.textContent || '') + '').trim();
              const btns = Array.from(document.querySelectorAll('button,a,[role=\"button\"]')).filter(isVisible);
              const cand = btns
                .map(b => {
                  const t = texts(b).slice(0, 90);
                  const aria = ((b.getAttribute && b.getAttribute('aria-disabled')) || '').toLowerCase();
                  const cls = ((b.getAttribute && b.getAttribute('class')) || '');
                  const dis = !!b.disabled || aria === 'true' || (cls + '').toLowerCase().includes('disabled');
                  const tl = t.toLowerCase();
                  const hit = tl.includes('apply') || tl.includes('quota') || tl.includes('increase') || tl.includes('request') ||
                    t.includes('\\u7533\\u8bf7') || t.includes('\\u914d\\u989d') || t.includes('\\u989d\\u5ea6') || t.includes('\\u63d0\\u989d');
                  return hit ? { t, dis } : null;
                })
                .filter(x => !!x)
                .slice(0, 12);

              const toasts = Array.from(document.querySelectorAll('.ant-message-notice,.ant-notification-notice,[role=\"alert\"]'))
                .filter(isVisible)
                .map(el => texts(el).slice(0, 120))
                .slice(0, 6);

              return {
                url: location.href,
                viewport: { w: window.innerWidth, h: window.innerHeight },
                dialogs: Array.from(document.querySelectorAll('[role=\"dialog\"],.ant-modal-content,.ant-modal,.modal')).filter(isVisible).length,
                candidates: cand,
                toasts
              };
            } catch (e) {
              return { error: String(e), url: location.href };
            }
        """
        try:
            dbg = page.run_js(dbg_js, timeout=6) or {}
        except Exception as e:
            dbg_err = str(e)
            dbg = {"dbg_error": dbg_err, "url": getattr(page, "url", "")}

        _debug_dump_quota(page, note="quota_form_not_open_dbg")
        # Always include dbg snippet; it will help identify disabled buttons / toasts / layout differences.
        return {
            "ok": False,
            "error": f"quota form did not open (dbg={json.dumps(dbg, ensure_ascii=True)[:600]})",
            "url": getattr(page, "url", ""),
        }

    # 3) Fill the modal form + agree + submit using JS (works across many UI libs).
    industry_json = json.dumps(industry)
    scenario_json = json.dumps(scenario)
    js = f"""
        return (async () => {{
          try {{
            const industry = {industry_json};
            const scenario = {scenario_json};
            const isVisible = (el) => {{
              if (!el) return false;
              const st = window.getComputedStyle(el);
              if (!st) return false;
              if (st.display === 'none' || st.visibility === 'hidden') return false;
              const r = el.getBoundingClientRect();
              return r && r.width > 0 && r.height > 0;
            }};
            const setVal = (el, v) => {{
              try {{
                const proto = Object.getPrototypeOf(el);
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                const setter = desc && desc.set;
                if (setter) setter.call(el, v);
                else el.value = v;
              }} catch (e) {{
                try {{ el.value = v; }} catch (e2) {{}}
              }}
              try {{ el.dispatchEvent(new Event('input', {{ bubbles: true }})); }} catch (e) {{}}
              try {{ el.dispatchEvent(new Event('change', {{ bubbles: true }})); }} catch (e) {{}}
              try {{ el.dispatchEvent(new Event('blur', {{ bubbles: true }})); }} catch (e) {{}}
            }};
            const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

            const findDialog = () => {{
              const dialogs = Array.from(
                document.querySelectorAll('[role="dialog"],.ant-modal-content,.ant-modal,.modal')
              ).filter(isVisible);
              return dialogs[0] || null;
            }};
            const scope = findDialog() || document;
            const visibles = (sel) =>
              Array.from(scope.querySelectorAll(sel))
                .filter(isVisible)
                .filter((el) => (el.getAttribute && (el.getAttribute('type') || '').toLowerCase()) !== 'hidden');

            let okIndustry = false;
            let okScenario = false;
            let agreed = false;
            let submitted = false;
            let submitDisabled = null;

            // Fill required textarea (Usage scenario).
            const ta =
              visibles('textarea')[0] ||
              visibles('input,textarea').find((el) => {{
                const ph = (el.getAttribute && (el.getAttribute('placeholder') || '')) || '';
                return ph.includes('\u573a\u666f') || ph.toLowerCase().includes('scenario');
              }}) ||
              null;
            if (ta) {{
              setVal(ta, scenario || 'Chatbot');
              okScenario = true;
            }}

            // Industry is required; if empty, open the select and pick the first enabled option.
            const industryInput =
              visibles('input').find((el) => {{
                const ph = (el.getAttribute && (el.getAttribute('placeholder') || '')) || '';
                return ph.includes('\\u884c\\u4e1a') || ph.toLowerCase().includes('industry');
              }}) || null;

            const getIndustrySelected = () => {{
              try {{
                const v = (industryInput && (industryInput.value || '') || '').trim();
                if (v) return v;

                const ph = ((industryInput && industryInput.getAttribute && industryInput.getAttribute('placeholder')) || '').trim();
                // Some AntD combobox/select variants show the selected value as the input placeholder.
                if (ph && !ph.includes('\\u8bf7\\u9009\\u62e9') && !ph.toLowerCase().includes('select')) return ph;

                const root = industryInput && industryInput.closest ? industryInput.closest('.ant-select') : null;
                const item = root ? root.querySelector('.ant-select-selection-item') : null;
                return item ? ((item.innerText || '').trim()) : '';
              }} catch (e) {{
                return '';
              }}
            }};

            if (industryInput) {{
              if (getIndustrySelected()) {{
                okIndustry = true;
              }} else {{
                try {{ industryInput.click(); }} catch (e) {{}}
                await sleep(250);
                const opt =
                  Array.from(document.querySelectorAll('.ant-select-item-option'))
                    .filter(isVisible)
                    .find((o) => !(o.classList && o.classList.contains('ant-select-item-option-disabled'))) ||
                  null;
                if (opt) {{
                  try {{ opt.click(); }} catch (e) {{}}
                  await sleep(150);
                }} else {{
                  // Fallback: type a keyword and press Enter (some selects are searchable).
                  try {{ setVal(industryInput, industry || 'Chatbot'); }} catch (e) {{}}
                  try {{ industryInput.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', bubbles: true }})); }} catch (e) {{}}
                  try {{ industryInput.dispatchEvent(new KeyboardEvent('keyup', {{ key: 'Enter', bubbles: true }})); }} catch (e) {{}}
                  await sleep(150);
                }}

                if (getIndustrySelected()) {{
                  okIndustry = true;
                }}
              }}
            }}

            // Optional: fill company to avoid hidden validation.
            const companyInput =
              visibles('input').find((el) => {{
                const ph = (el.getAttribute && (el.getAttribute('placeholder') || '')) || '';
                return ph.includes('\\u516c\\u53f8') || ph.toLowerCase().includes('company');
              }}) || null;
            if (companyInput && !((companyInput.value || '').trim())) {{
              try {{ setVal(companyInput, 'Acme'); }} catch (e) {{}}
            }}

            // Optional: pick a job/role if it's a select and empty.
            const jobInput =
              visibles('input').find((el) => {{
                const ph = (el.getAttribute && (el.getAttribute('placeholder') || '')) || '';
                return (
                  ph.includes('\\u804c\\u52a1') ||
                  ph.toLowerCase().includes('job') ||
                  ph.toLowerCase().includes('position')
                );
              }}) || null;
            try {{
              const root = jobInput && jobInput.closest ? jobInput.closest('.ant-select') : null;
              const item = root ? root.querySelector('.ant-select-selection-item') : null;
              const hasJob = item && ((item.innerText || '').trim().length > 0);
              if (jobInput && !hasJob) {{
                try {{ jobInput.click(); }} catch (e) {{}}
                await sleep(250);
                const opt2 =
                  Array.from(document.querySelectorAll('.ant-select-item-option'))
                    .filter(isVisible)
                    .find((o) => !(o.classList && o.classList.contains('ant-select-item-option-disabled'))) ||
                  null;
                if (opt2) {{
                  try {{ opt2.click(); }} catch (e) {{}}
                  await sleep(150);
                }}
              }}
            }} catch (e) {{}}

            // Agree checkbox (must be checked).
            // HF/container runs are often pickier about click targets, so we try:
            //  1) agreement-text anchored search
            //  2) click all checkboxes in the dialog/form scope
            //  3) programmatically set checked=true + dispatch events (last resort)
            const agreeNeedle1 = '\\u7528\\u6237\\u534f\\u8bae'; // user agreement
            const agreeNeedle2 = '\\u9690\\u79c1\\u653f\\u7b56'; // privacy policy
            const agreeNeedle3 = '\\u6211\\u5df2\\u9605\\u8bfb'; // I have read

            let agreeDebug = {{
              roleCount: 0,
              inputCount: 0,
              checkedCount: 0,
              sample: []
            }};

            const clickIfPossible = (el) => {{
              try {{
                const tgt =
                  (el.querySelector && (el.querySelector('.ant-checkbox-inner') || el.querySelector('.ant-checkbox') || el.querySelector('.checkbox') || el)) ||
                  el;
                if (tgt && tgt.click) tgt.click();
              }} catch (e) {{}}
            }};

            const isCheckedAny = () => {{
              try {{
                const inputs = Array.from(scope.querySelectorAll('input[type=\"checkbox\"]'));
                if (inputs.some((i) => !!i.checked)) return true;
                if (scope.querySelector && scope.querySelector('.ant-checkbox-checked')) return true;
                const roles = Array.from(scope.querySelectorAll('[role=\"checkbox\"]'));
                if (roles.some((r) => ((r.getAttribute('aria-checked') || '').toLowerCase() === 'true'))) return true;
              }} catch (e) {{}}
              return false;
            }};

            const findAgreementContainer = () => {{
              try {{
                const all = Array.from(scope.querySelectorAll('*')).filter(isVisible);
                const hit = all.find((el) => {{
                  const t = (el.innerText || '').trim();
                  if (!t) return false;
                  const tl = t.toLowerCase();
                  return (
                    t.includes(agreeNeedle1) ||
                    t.includes(agreeNeedle2) ||
                    t.includes(agreeNeedle3) ||
                    tl.includes('agree') ||
                    tl.includes('privacy') ||
                    tl.includes('terms')
                  );
                }});
                if (!hit) return null;
                let cur = hit;
                for (let i = 0; i < 10 && cur; i += 1) {{
                  if (cur.getAttribute && cur.getAttribute('role') === 'checkbox') return cur;
                  if (cur.querySelector && cur.querySelector('input[type=\"checkbox\"]')) return cur;
                  cur = cur.parentElement;
                }}
                return hit;
              }} catch (e) {{
                return null;
              }}
            }};

            const markChecked = (cb) => {{
              try {{
                cb.checked = true;
              }} catch (e) {{}}
              try {{ cb.dispatchEvent(new Event('input', {{ bubbles: true }})); }} catch (e) {{}}
              try {{ cb.dispatchEvent(new Event('change', {{ bubbles: true }})); }} catch (e) {{}}
              try {{ cb.dispatchEvent(new MouseEvent('click', {{ bubbles: true }})); }} catch (e) {{}}
            }};

            // Nudge scroll so the checkbox area becomes visible.
            try {{
              const dlg = (findDialog && findDialog()) || null;
              const body = dlg && dlg.querySelector ? (dlg.querySelector('.ant-modal-body') || dlg) : null;
              if (body) body.scrollTop = 1e9;
            }} catch (e) {{}}

            const agreeContainer = findAgreementContainer();
            if (agreeContainer) {{
              clickIfPossible(agreeContainer);
              await sleep(150);
            }}

            // Try clicking all checkboxes in scope.
            try {{
              const roleBoxes = Array.from(scope.querySelectorAll('[role=\"checkbox\"]')).filter(isVisible);
              agreeDebug.roleCount = roleBoxes.length;
              for (const r of roleBoxes.slice(0, 8)) {{
                const aria = ((r.getAttribute && r.getAttribute('aria-checked')) || '').toLowerCase();
                if (aria !== 'true') {{
                  clickIfPossible(r);
                  await sleep(120);
                }}
              }}
            }} catch (e) {{}}

            try {{
              const inputs = Array.from(scope.querySelectorAll('input[type=\"checkbox\"]'));
              agreeDebug.inputCount = inputs.length;
              for (const cb of inputs.slice(0, 8)) {{
                if (cb && !cb.checked) {{
                  const wrap = cb.closest ? (cb.closest('label') || cb.closest('.ant-checkbox-wrapper') || cb.closest('div') || cb.parentElement) : cb.parentElement;
                  if (wrap) clickIfPossible(wrap);
                  await sleep(120);
                  if (!cb.checked) markChecked(cb);
                  await sleep(80);
                }}
              }}
            }} catch (e) {{}}

            agreed = isCheckedAny();
            try {{
              const inputs2 = Array.from(scope.querySelectorAll('input[type=\"checkbox\"]'));
              const checked2 = inputs2.filter((i) => !!i.checked).length;
              agreeDebug.checkedCount = checked2;
              const labels = Array.from(scope.querySelectorAll('label,span,div'))
                .filter(isVisible)
                .map((el) => ((el.innerText || '').trim()).slice(0, 90))
                .filter((t) => t && (t.toLowerCase().includes('agree') || t.includes(agreeNeedle1) || t.includes(agreeNeedle2) || t.includes(agreeNeedle3)))
                .slice(0, 4);
              agreeDebug.sample = labels;
            }} catch (e) {{}}

            await sleep(350);

            // Submit button.
            const btns = visibles('button');
            const submitBtn =
              btns.find((b) => b.classList && b.classList.contains('ant-btn-primary')) ||
              btns.find((b) => {{
                const t = (b.innerText || '').trim();
                return t.includes('\u63d0\u4ea4') || t.toLowerCase().includes('submit');
              }}) ||
              null;
            if (submitBtn) {{
              const aria = ((submitBtn.getAttribute && submitBtn.getAttribute('aria-disabled')) || '').toLowerCase();
              const cls = (submitBtn.getAttribute && (submitBtn.getAttribute('class') || '')) || '';
              const disabled = !!submitBtn.disabled || aria === 'true' || cls.includes('disabled') || cls.includes('is-disabled');
              submitDisabled = disabled;
              if (!disabled) {{
                try {{ submitBtn.click(); submitted = true; }} catch (e) {{}}
              }}
            }}

            return {{
              okIndustry,
              okScenario,
              agreed,
              agreeDebug,
              submitted,
              submitDisabled,
              url: location.href
            }};
          }} catch (e) {{
            return {{ error: String(e), url: location.href }};
          }}
        }})();
    """

    try:
        form_result = page.run_js(js, timeout=8) or {}
    except Exception as e:
        form_result = {"error": str(e), "url": getattr(page, "url", "")}

    wait_for_page_stable(page, timeout=6)
    _debug_dump_quota(page, note="after_submit")

    if isinstance(form_result, dict) and form_result.get("error"):
        return {"ok": False, "error": str(form_result.get("error")), "form": form_result}
    if isinstance(form_result, dict) and not form_result.get("submitted"):
        if form_result.get("okIndustry") is False:
            return {"ok": False, "error": "industry field not selected", "form": form_result}
        if form_result.get("okScenario") is False:
            return {"ok": False, "error": "usage scenario field not found/filled", "form": form_result}
        if form_result.get("agreed") is False:
            dbg = ""
            try:
                dbg = json.dumps(form_result.get("agreeDebug") or {}, ensure_ascii=True)[:400]
            except Exception:
                dbg = ""
            if dbg:
                return {"ok": False, "error": f"agreement checkbox not found/clicked (debug={dbg})", "form": form_result}
            return {"ok": False, "error": "agreement checkbox not found/clicked", "form": form_result}
        if form_result.get("submitDisabled") is True:
            return {
                "ok": False,
                "error": "submit button disabled (required fields missing or UI not updated yet)",
                "form": form_result,
            }
        return {"ok": False, "error": "submit button not found/clicked", "form": form_result}

    # 4) Confirm success: wait for the dialog to close (or a success toast).
    ok = False
    try:
        if wait_for_element(page, "text:\u63d0\u4ea4\u6210\u529f", timeout=2) or wait_for_element(page, "text:\u5df2\u63d0\u4ea4", timeout=2):
            ok = True
    except Exception:
        ok = False

    if not ok:
        start = time.time()
        while time.time() - start < 12:
            if not _quota_form_visible():
                ok = True
                break
            time.sleep(0.3)

    applied_at = datetime.now(timezone.utc).isoformat()
    if ok:
        return {"ok": True, "applied_at": applied_at, "form": form_result}

    err = ""
    if isinstance(form_result, dict):
        err = str(form_result.get("error") or "") or "quota submit not confirmed"
    return {"ok": False, "error": err, "applied_at": applied_at, "form": form_result}
