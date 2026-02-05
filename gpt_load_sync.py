"""
Submit generated API keys to an existing GPT-Load deployment (management API).

This project generates LongCat API keys and appends them to temp/longcat_keys.txt.
If AUTH_KEY is provided, we can automatically import those keys into a target group
on a remote GPT-Load instance via its /api endpoints.

No server-side modifications are required (and are not allowed in this setup).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


DEFAULT_BASE_URL = "https://great429gptload.zeabur.app"
DEFAULT_GROUP_NAME = "#pinhaofan"


class GptLoadSyncError(RuntimeError):
    pass


class _RetryableError(Exception):
    pass


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sanitize_group_for_filename(group_name: str) -> str:
    s = (group_name or "").strip()
    if s.startswith("#"):
        s = s[1:]
    # Keep it simple and ASCII for filenames.
    out = []
    for ch in s:
        if ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch in ("-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "group"


def _normalize_base_url(base_url: str) -> str:
    base_url = (base_url or "").strip()
    while base_url.endswith("/"):
        base_url = base_url[:-1]
    return base_url


def _read_keys_from_file(path: str | Path) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8", errors="ignore")
    keys: list[str] = []
    for line in text.splitlines():
        k = (line or "").strip()
        if k:
            keys.append(k)
    return keys


def _dedupe_keep_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out


def _default_state_path(group_name: str) -> Path:
    safe = _sanitize_group_for_filename(group_name)
    return Path("temp") / f"gpt_load_synced_{safe}.sha256"


def _load_state_hashes(path: Path) -> set[str]:
    try:
        if not path.exists():
            return set()
        # One sha256 hex per line.
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return {ln.strip() for ln in lines if ln.strip()}
    except Exception:
        return set()


def _append_state_hashes(path: Path, hashes: Iterable[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    with path.open("a", encoding="utf-8", newline="") as f:
        for h in hashes:
            h = (h or "").strip()
            if h:
                f.write(h + "\n")


def _request_json(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    payload: Any = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    data = None
    req_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req_headers.setdefault("Accept", "application/json")

    req = urllib.request.Request(url, data=data, method=method.upper())
    for k, v in req_headers.items():
        req.add_header(k, v)

    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read() or b"{}"
            return resp.status, json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            parsed = {"code": "HTTP_ERROR", "message": raw.decode("utf-8", errors="replace")[:500]}
        return int(e.code), parsed
    except urllib.error.URLError as e:
        raise _RetryableError(str(e)) from e


def _retry(fn, *, retries: int = 3, base_sleep: float = 0.8):
    last: Optional[BaseException] = None
    for attempt in range(retries):
        try:
            return fn()
        except _RetryableError as e:
            last = e
            sleep_s = base_sleep * (2**attempt) + random.uniform(0, 0.25)
            time.sleep(sleep_s)
    if last:
        raise last
    raise RuntimeError("unreachable")


@dataclass(frozen=True)
class TaskStatus:
    task_type: str = ""
    is_running: bool = False
    group_name: str = ""
    processed: int = 0
    total: int = 0
    result: Any = None
    error: str = ""

    @staticmethod
    def from_api(data: dict[str, Any]) -> "TaskStatus":
        return TaskStatus(
            task_type=str(data.get("task_type") or ""),
            is_running=bool(data.get("is_running") or False),
            group_name=str(data.get("group_name") or ""),
            processed=int(data.get("processed") or 0),
            total=int(data.get("total") or 0),
            result=data.get("result"),
            error=str(data.get("error") or ""),
        )


class GptLoadClient:
    def __init__(self, base_url: str, auth_key: str, *, timeout: float = 30.0):
        self.base_url = _normalize_base_url(base_url)
        self.auth_key = (auth_key or "").strip()
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        # GPT-Load's admin API accepts Bearer, X-Api-Key, X-Goog-Api-Key, or ?key=...
        return {"Authorization": f"Bearer {self.auth_key}"}

    def list_groups(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/groups"

        def _do():
            status, body = _request_json("GET", url, headers=self._headers(), timeout=self.timeout)
            if status >= 500:
                raise _RetryableError(f"GET /api/groups {status}")
            if status != 200:
                raise GptLoadSyncError(f"GET /api/groups failed: HTTP {status} {body}")
            if body.get("code") != 0:
                raise GptLoadSyncError(f"GET /api/groups failed: {body}")
            data = body.get("data")
            return data if isinstance(data, list) else []

        return _retry(_do, retries=3)

    def resolve_group_id(self, group_name: str) -> int:
        target = (group_name or "").strip()
        if not target:
            raise GptLoadSyncError("Missing group_name")
        target_no_hash = target[1:] if target.startswith("#") else target

        groups = self.list_groups()
        for g in groups:
            name = str(g.get("name") or "")
            display = str(g.get("display_name") or "")
            if target in (name, display):
                return int(g.get("id") or 0)
            if target_no_hash and target_no_hash in (name, display):
                return int(g.get("id") or 0)

        # Small diagnostic (do not dump everything).
        sample = []
        for g in groups[:10]:
            sample.append({"id": g.get("id"), "name": g.get("name"), "display_name": g.get("display_name")})
        raise GptLoadSyncError(f"Group not found: {target}. Sample groups: {sample}")

    def add_keys_async(self, group_id: int, keys_text: str) -> TaskStatus:
        url = f"{self.base_url}/api/keys/add-async"
        payload = {"group_id": int(group_id), "keys_text": keys_text or ""}

        def _do():
            status, body = _request_json("POST", url, headers=self._headers(), payload=payload, timeout=self.timeout)
            if status in (429,) or status >= 500:
                raise _RetryableError(f"POST /api/keys/add-async {status}")
            if status != 200:
                raise GptLoadSyncError(f"POST /api/keys/add-async failed: HTTP {status} {body}")
            if body.get("code") != 0:
                raise GptLoadSyncError(f"POST /api/keys/add-async failed: {body}")
            data = body.get("data") or {}
            if isinstance(data, dict):
                return TaskStatus.from_api(data)
            return TaskStatus()

        return _retry(_do, retries=3)

    def get_task_status(self) -> TaskStatus:
        url = f"{self.base_url}/api/tasks/status"

        def _do():
            status, body = _request_json("GET", url, headers=self._headers(), timeout=self.timeout)
            if status >= 500:
                raise _RetryableError(f"GET /api/tasks/status {status}")
            if status != 200:
                raise GptLoadSyncError(f"GET /api/tasks/status failed: HTTP {status} {body}")
            if body.get("code") != 0:
                raise GptLoadSyncError(f"GET /api/tasks/status failed: {body}")
            data = body.get("data") or {}
            return TaskStatus.from_api(data if isinstance(data, dict) else {})

        return _retry(_do, retries=3)


def sync_keys_to_gpt_load(
    keys: Iterable[str],
    *,
    auth_key: str,
    group_name: str = DEFAULT_GROUP_NAME,
    base_url: str = DEFAULT_BASE_URL,
    state_path: Optional[str | Path] = None,
    force: bool = False,
    poll: bool = True,
    poll_timeout_s: float = 120.0,
    poll_interval_s: float = 1.0,
    log=None,
) -> dict[str, Any]:
    """
    Import keys into a remote GPT-Load group.

    Returns a dict of stats (added/ignored if available).
    """
    auth_key = (auth_key or "").strip()
    if not auth_key:
        raise GptLoadSyncError("Missing auth_key (AUTH_KEY)")

    base_url = _normalize_base_url(base_url or DEFAULT_BASE_URL)
    group_name = (group_name or DEFAULT_GROUP_NAME).strip()

    keys_list = _dedupe_keep_order((k or "").strip() for k in keys if (k or "").strip())
    if not keys_list:
        return {"sent": 0, "skipped": 0, "reason": "no_keys"}

    st_path = Path(state_path) if state_path else _default_state_path(group_name)
    old_hashes = set() if force else _load_state_hashes(st_path)
    new_keys: list[str] = []
    new_hashes: list[str] = []
    for k in keys_list:
        h = _sha256_hex(k)
        if not force and h in old_hashes:
            continue
        new_keys.append(k)
        new_hashes.append(h)

    if not new_keys:
        return {"sent": 0, "skipped": len(keys_list), "reason": "already_synced"}

    client = GptLoadClient(base_url=base_url, auth_key=auth_key, timeout=30.0)
    group_id = client.resolve_group_id(group_name)

    keys_text = "\n".join(new_keys)
    if log:
        log.info(
            f"GPT-Load sync: importing {len(new_keys)} key(s) to group {group_name} (id={group_id})",
            icon="sync",
        )

    task = client.add_keys_async(group_id, keys_text)

    final_status: Optional[TaskStatus] = None
    if poll:
        deadline = time.time() + float(poll_timeout_s)
        while time.time() < deadline:
            cur = client.get_task_status()
            if not cur.is_running:
                final_status = cur
                break
            time.sleep(float(poll_interval_s))

    # Only mark as "synced" if we are confident the import finished successfully.
    # If polling is disabled, we can only assume "submitted" (server accepted request).
    should_mark_synced = (not poll) or (final_status is not None and not final_status.error)
    if should_mark_synced:
        _append_state_hashes(st_path, new_hashes)

    out: dict[str, Any] = {
        "sent": len(new_keys),
        "skipped": len(keys_list) - len(new_keys),
        "group_id": group_id,
        "group_name": group_name,
        "base_url": base_url,
        "task": task.__dict__,
    }

    if final_status:
        out["final_status"] = final_status.__dict__
        if final_status.error:
            out["error"] = final_status.error
        if isinstance(final_status.result, dict):
            out.update(final_status.result)
    elif poll:
        out["error"] = "poll_timeout"
        out["poll_timeout_s"] = float(poll_timeout_s)

    if log:
        # Avoid printing raw keys; only counts.
        if final_status and final_status.error:
            log.warning(f"GPT-Load sync finished with error: {final_status.error}")
        elif poll and final_status is None:
            log.warning("GPT-Load sync: submitted, but polling timed out (task may still be running)")
        else:
            added = None
            ignored = None
            try:
                if final_status and isinstance(final_status.result, dict):
                    added = int(final_status.result.get("added_count"))  # type: ignore[union-attr]
                    ignored = int(final_status.result.get("ignored_count"))  # type: ignore[union-attr]
            except Exception:
                pass
            if added is not None and ignored is not None:
                log.success(f"GPT-Load import done: added={added}, ignored={ignored}")
            else:
                log.success("GPT-Load import done")

    return out


def sync_keys_file_to_gpt_load(
    keys_file: str | Path,
    *,
    auth_key: str,
    group_name: str = DEFAULT_GROUP_NAME,
    base_url: str = DEFAULT_BASE_URL,
    state_path: Optional[str | Path] = None,
    force: bool = False,
    poll: bool = True,
    poll_timeout_s: float = 120.0,
    poll_interval_s: float = 1.0,
    log=None,
) -> dict[str, Any]:
    keys = _read_keys_from_file(keys_file)
    return sync_keys_to_gpt_load(
        keys,
        auth_key=auth_key,
        group_name=group_name,
        base_url=base_url,
        state_path=state_path,
        force=force,
        poll=poll,
        poll_timeout_s=poll_timeout_s,
        poll_interval_s=poll_interval_s,
        log=log,
    )
