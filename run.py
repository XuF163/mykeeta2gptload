"""
Entry point for the trimmed LongCat key generator.

Default (no args):
  - Generate N LongCat API keys (N from config.toml [longcat].keys_count)
  - Append keys to temp/longcat_keys.txt
  - Append records to temp/longcat_keys.csv
  - Best-effort sync each new key to GPT-Load (if enabled in config.toml [gpt_load])
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

from logger import log
from config import (
    LONGCAT_KEYS_COUNT,
    LONGCAT_KEYS_FILE,
    LONGCAT_CSV_PATH,
    LONGCAT_PASSPORT_LOGIN_URL,
    GPT_LOAD_SYNC_ENABLED,
    GPT_LOAD_BASE_URL,
    GPT_LOAD_GROUP_NAME,
    GPT_LOAD_AUTH_KEY,
    GPT_LOAD_FORCE,
    GPT_LOAD_POLL,
    GPT_LOAD_POLL_TIMEOUT_S,
    GPT_LOAD_POLL_INTERVAL_S,
    GPT_LOAD_STATE_FILE,
)


def _reset_keys_file(path: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")
    return p


def _append_key_line(path: Path, key: str) -> None:
    k = (key or "").strip()
    if not k:
        return
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(k + "\n")


def _maybe_sync_keys_to_gpt_load(keys: list[str]) -> None:
    """
    Best-effort: import newly generated keys into a remote GPT-Load deployment.

    Manual command gpt-load-sync always runs; auto sync is controlled by:
      config.toml: [gpt_load].enabled = true
    """

    auth_key = (GPT_LOAD_AUTH_KEY or "").strip()
    if not GPT_LOAD_SYNC_ENABLED:
        # Log once to avoid confusion ("manual works, auto doesn't").
        try:
            if not getattr(_maybe_sync_keys_to_gpt_load, "_disabled_logged", False) and any(
                (k or "").strip() for k in (keys or [])
            ):
                setattr(_maybe_sync_keys_to_gpt_load, "_disabled_logged", True)
                log.info("GPT-Load 自动提交未开启: 请在 config.toml 的 [gpt_load] 设置 enabled = true", icon="sync")
        except Exception:
            pass
        return

    if not auth_key:
        # Log once; in Docker/Zeabur we expect auth_key via env most of the time.
        try:
            if not getattr(_maybe_sync_keys_to_gpt_load, "_missing_auth_logged", False) and any(
                (k or "").strip() for k in (keys or [])
            ):
                setattr(_maybe_sync_keys_to_gpt_load, "_missing_auth_logged", True)
                log.warning(
                    "GPT-Load 自动提交已开启但缺少 auth_key: 请设置 config.toml [gpt_load].auth_key 或环境变量 GPT_LOAD_AUTH_KEY",
                    icon="sync",
                )
        except Exception:
            pass
        return

    try:
        from gpt_load_sync import sync_keys_to_gpt_load

        sync_keys_to_gpt_load(
            keys,
            auth_key=auth_key,
            group_name=(GPT_LOAD_GROUP_NAME or "#pinhaofan").strip(),
            base_url=(GPT_LOAD_BASE_URL or "https://great429gptload.zeabur.app").strip(),
            state_path=(GPT_LOAD_STATE_FILE or "").strip() or None,
            force=bool(GPT_LOAD_FORCE),
            poll=bool(GPT_LOAD_POLL),
            poll_timeout_s=float(GPT_LOAD_POLL_TIMEOUT_S),
            poll_interval_s=float(GPT_LOAD_POLL_INTERVAL_S),
            log=log,
        )
    except Exception as e:
        log.warning(f"GPT-Load sync failed (best-effort): {e}")


def cmd_generate(count: int) -> list[dict]:
    from longcat_automation import create_longcat_account_and_api_key, DEFAULT_PASSPORT_LOGIN_URL

    passport_url = (LONGCAT_PASSPORT_LOGIN_URL or DEFAULT_PASSPORT_LOGIN_URL).strip()

    keys_path = _reset_keys_file(LONGCAT_KEYS_FILE or "temp/longcat_keys.txt")

    results: list[dict] = []
    for i in range(count):
        if i > 0:
            time.sleep(random.uniform(0.5, 1.5))

        r = create_longcat_account_and_api_key(
            passport_login_url=passport_url,
            csv_path=LONGCAT_CSV_PATH or "temp/longcat_keys.csv",
        )
        results.append(r)

        api_key = r.get("api_key", "")
        log.success(f"LongCat API Key: {api_key}")
        _append_key_line(keys_path, api_key)

        # Realtime-ish: submit right after generation.
        _maybe_sync_keys_to_gpt_load([api_key])

    return results


def cmd_gpt_load_sync(keys_file: str) -> dict:
    from gpt_load_sync import sync_keys_file_to_gpt_load

    auth_key = (GPT_LOAD_AUTH_KEY or "").strip()
    if not auth_key:
        raise SystemExit("Missing config.toml [gpt_load].auth_key")

    return sync_keys_file_to_gpt_load(
        keys_file,
        auth_key=auth_key,
        group_name=(GPT_LOAD_GROUP_NAME or "#pinhaofan").strip(),
        base_url=(GPT_LOAD_BASE_URL or "https://great429gptload.zeabur.app").strip(),
        state_path=(GPT_LOAD_STATE_FILE or "").strip() or None,
        force=bool(GPT_LOAD_FORCE),
        poll=bool(GPT_LOAD_POLL),
        poll_timeout_s=float(GPT_LOAD_POLL_TIMEOUT_S),
        poll_interval_s=float(GPT_LOAD_POLL_INTERVAL_S),
        log=log,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="Generate LongCat API key(s) (default).")
    p_run.add_argument("--count", type=int, default=None, help="Override [longcat].keys_count")

    p_sync = sub.add_parser("gpt-load-sync", help="Submit keys file to GPT-Load group.")
    p_sync.add_argument("keys_file", nargs="?", default=LONGCAT_KEYS_FILE or "temp/longcat_keys.txt")

    # Default to "run" if no subcommand is provided.
    if not argv or (argv and argv[0].startswith("-")):
        argv = ["run", *argv]

    args = ap.parse_args(argv)

    if args.cmd == "gpt-load-sync":
        res = cmd_gpt_load_sync(args.keys_file)
        print(json.dumps(res, ensure_ascii=False))
        return 0

    if args.cmd == "run":
        count = args.count if args.count is not None else int(LONGCAT_KEYS_COUNT or 1)
        if count <= 0:
            log.warning("count <= 0, nothing to do.")
            print("[]")
            return 0
        results = cmd_generate(count)
        print(json.dumps(results, ensure_ascii=False))
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
