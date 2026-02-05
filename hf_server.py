"""
HF Spaces Docker runtime adapter.

HuggingFace Spaces expects a long-running process that listens on $PORT.
This project is a "job" (generate keys then exit), so we provide a tiny HTTP
server to keep the container alive and allow triggering runs.

Endpoints:
  GET  /health   -> 200 OK
  GET  /status   -> JSON (running/last exit code + tail)
  POST /run      -> start a background run (if not already running)

The actual job is executed via:
  xvfb-run -a uv run python run.py

so Chromium can run in the container without a real display.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


def _now() -> float:
    return time.time()


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running: bool = False
        self.last_exit_code: int | None = None
        self.last_started_at: float | None = None
        self.last_finished_at: float | None = None
        self.last_log_tail: str = ""


STATE = _State()


def _tail_text(path: str, max_bytes: int = 24_000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start, os.SEEK_SET)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _as_int_env(name: str, default: int) -> int:
    v = (os.getenv(name, "") or "").strip()
    if not v:
        return default
    try:
        return int(float(v))
    except Exception:
        return default


def _run_job_background() -> None:
    log_path = "/tmp/job.log"
    with STATE.lock:
        STATE.running = True
        STATE.last_started_at = _now()
        STATE.last_finished_at = None
        STATE.last_exit_code = None
        STATE.last_log_tail = ""

    # Run the job and capture output for /status.
    # Force a larger virtual screen so the site doesn't switch into a mobile layout.
    cmd = ["sh", "-lc", "xvfb-run -a -s \"-screen 0 1920x1080x24\" uv run python run.py"]
    exit_code: int | None = None
    try:
        with open(log_path, "wb") as out:
            p = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
            exit_code = int(p.wait())
    except Exception:
        exit_code = 1
    finally:
        tail = _tail_text(log_path)
        with STATE.lock:
            STATE.running = False
            STATE.last_exit_code = exit_code
            STATE.last_finished_at = _now()
            STATE.last_log_tail = tail


def _maybe_start_job() -> bool:
    with STATE.lock:
        if STATE.running:
            return False
        t = threading.Thread(target=_run_job_background, daemon=True)
        t.start()
        return True


def _scheduler_loop() -> None:
    """
    Periodically trigger the job so the Space is "always working".

    Configure via env:
      - AUTO_RUN_ON_START=1
      - RUN_EVERY_SECONDS=3600  (or RUN_EVERY_MINUTES)

    If interval <= 0, scheduler is disabled.
    """
    auto = (os.getenv("AUTO_RUN_ON_START", "") or "").strip().lower() in ("1", "true", "yes", "y", "on")
    every_s = _as_int_env("RUN_EVERY_SECONDS", 0)
    if every_s <= 0:
        every_m = _as_int_env("RUN_EVERY_MINUTES", 0)
        if every_m > 0:
            every_s = every_m * 60

    if auto:
        _maybe_start_job()

    if every_s <= 0:
        return

    # Small initial delay to let the server come up.
    time.sleep(2.0)
    while True:
        # If a run is already executing, just wait.
        _maybe_start_job()
        time.sleep(max(5, every_s))


class Handler(BaseHTTPRequestHandler):
    server_version = "hf-server/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep stdout clean; Spaces shows container logs elsewhere.
        return

    def _send(self, code: int, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/?"):
            html = (
                "<!doctype html><html><head><meta charset='utf-8'/>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
                "<title>mykeeta2gptload</title>"
                "<style>body{font-family:ui-monospace,Menlo,Consolas,monospace;padding:24px;max-width:880px}"
                "button{padding:10px 14px;border:1px solid #111;background:#111;color:#fff;cursor:pointer}"
                "pre{white-space:pre-wrap;background:#f6f6f6;padding:12px;border-radius:8px;}"
                "</style></head><body>"
                "<h2>mykeeta2gptload</h2>"
                "<p>This container runs a LongCat key generation job on demand.</p>"
                "<p><button onclick='run()'>Run Job</button> <button onclick='refresh()'>Refresh Status</button></p>"
                "<pre id='out'>Loading...</pre>"
                "<script>"
                "async function refresh(){"
                " const r=await fetch('/status'); const j=await r.json();"
                " document.getElementById('out').textContent=JSON.stringify(j,null,2);"
                "}"
                "async function run(){"
                " const r=await fetch('/run',{method:'POST'}); const j=await r.json();"
                " document.getElementById('out').textContent=JSON.stringify(j,null,2);"
                "}"
                "refresh();"
                "</script></body></html>"
            ).encode("utf-8")
            self._send(200, html, "text/html; charset=utf-8")
            return

        if self.path == "/health":
            self._send(200, b"ok\n")
            return

        if self.path == "/status":
            with STATE.lock:
                payload = {
                    "running": STATE.running,
                    "last_exit_code": STATE.last_exit_code,
                    "last_started_at": STATE.last_started_at,
                    "last_finished_at": STATE.last_finished_at,
                    "log_tail": STATE.last_log_tail[-12_000:],
                }
            self._send_json(200, payload)
            return

        self._send(404, b"not found\n")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/run":
            self._send(404, b"not found\n")
            return

        with STATE.lock:
            if STATE.running:
                self._send_json(200, {"started": False, "reason": "already_running"})
                return
            started = _maybe_start_job()
            self._send_json(200, {"started": bool(started)})


def main() -> int:
    port = int(os.getenv("PORT", "7860"))
    host = "0.0.0.0"

    # Optional: periodic runner for "always on" Spaces.
    threading.Thread(target=_scheduler_loop, daemon=True).start()

    httpd = HTTPServer((host, port), Handler)
    print(f"[hf_server] listening on http://{host}:{port}", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
