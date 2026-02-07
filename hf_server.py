"""
HF Spaces Docker runtime adapter.

HuggingFace Spaces expects a long-running process that listens on $PORT.
This project is a "job" (generate keys then exit), so we provide a tiny HTTP
server to keep the container alive and allow triggering runs.

Endpoints:
  GET  /health   -> 200 OK
  GET  /status   -> JSON (running/last exit code + tail)
  GET  /log      -> HTML (key generator run + status)
  POST /run      -> start a background run (if not already running)

The actual job is executed via:
  xvfb-run -a uv run python run.py

so Chromium can run in the container without a real display.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.parse
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection
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


def _wait_for_tcp(host: str, port: int, timeout_s: float = 15.0) -> bool:
    """
    Small helper to wait for an internal service port to become reachable.
    """
    deadline = _now() + max(0.1, timeout_s)
    while _now() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


GPT_LOAD_INTERNAL_HOST = "127.0.0.1"
GPT_LOAD_INTERNAL_PORT = 3001
GPT_LOAD_INTERNAL_BASE = f"http://{GPT_LOAD_INTERNAL_HOST}:{GPT_LOAD_INTERNAL_PORT}"


class _GptLoadState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen[bytes] | None = None
        self.last_start_error: str = ""
        self.restart_count: int = 0
        self.last_probe_ok_at: float | None = None
        self.last_started_at: float | None = None
        self.last_restart_at: float | None = None
        self.last_exit_code: int | None = None


GPT_LOAD = _GptLoadState()

# If gpt-load is slow to boot (DB cold start), avoid restart storms.
GPT_LOAD_STARTUP_GRACE_S = _as_int_env("GPT_LOAD_STARTUP_GRACE_S", 25)
GPT_LOAD_RESTART_COOLDOWN_S = _as_int_env("GPT_LOAD_RESTART_COOLDOWN_S", 30)


def _summarize_database_dsn(dsn: str) -> dict[str, str]:
    """
    Return a non-sensitive DSN summary for debugging persistence issues.
    """
    dsn = (dsn or "").strip()
    if not dsn:
        return {"mode": "sqlite", "host": "", "db": ""}

    # URL DSN: postgres://user:pass@host:5432/dbname?...
    try:
        u = urllib.parse.urlparse(dsn)
        if u.scheme and u.netloc:
            db = (u.path or "").lstrip("/")
            return {"mode": u.scheme, "host": u.hostname or "", "db": db}
    except Exception:
        pass

    # key=value DSN: host=... user=... dbname=... sslmode=...
    host = ""
    db = ""
    m = re.search(r"(?:^|\s)host=([^\s]+)", dsn)
    if m:
        host = m.group(1).strip()
    m = re.search(r"(?:^|\s)dbname=([^\s]+)", dsn)
    if m:
        db = m.group(1).strip()
    return {"mode": "dsn", "host": host, "db": db}


def _start_gpt_load_once() -> None:
    """
    Start gpt-load as an internal service and reverse-proxy it from this server.

    HF Spaces only exposes one external port ($PORT), so we keep gpt-load on
    127.0.0.1:3001 and proxy it.
    """
    with GPT_LOAD.lock:
        if GPT_LOAD.proc is not None and GPT_LOAD.proc.poll() is None:
            return

        env = os.environ.copy()
        env["HOST"] = GPT_LOAD_INTERNAL_HOST
        env["PORT"] = str(GPT_LOAD_INTERNAL_PORT)
        env.setdefault("TZ", "Asia/Shanghai")

        # Use the same secret for both:
        # - gpt-load management API/UI auth
        # - mykeeta -> gpt-load import auth
        auth_key = (env.get("GPT_LOAD_AUTH_KEY") or "").strip()
        env["AUTH_KEY"] = auth_key or env.get("AUTH_KEY", "").strip() or "change-me"
        env["ENCRYPTION_KEY"] = (env.get("GPT_LOAD_ENCRYPTION_KEY") or env.get("ENCRYPTION_KEY") or "").strip()

        # Prefer an explicit DSN to avoid SQLite locking under concurrency.
        # Users can set this in HF Space Secrets/Variables.
        # (gpt-load uses SQLite at ./data/gpt-load.db when DATABASE_DSN is empty.)
        env["DATABASE_DSN"] = (env.get("GPT_LOAD_DATABASE_DSN") or env.get("DATABASE_DSN") or "").strip()
        db_summary = _summarize_database_dsn(env["DATABASE_DSN"])

        try:
            # The binary is copied into the image in Dockerfile.
            # Log to a file so /status can show something helpful on failures.
            gpt_log_path = "/tmp/gpt-load.log"
            out = open(gpt_log_path, "ab", buffering=0)
            out.write(
                (
                    f"[hf_server] starting gpt-load: db_mode={db_summary['mode']} "
                    f"db_host={db_summary['host']} db_name={db_summary['db']}\n"
                ).encode("utf-8", errors="replace")
            )
            GPT_LOAD.proc = subprocess.Popen(["gpt-load"], stdout=out, stderr=subprocess.STDOUT, env=env)
            GPT_LOAD.last_start_error = ""
            GPT_LOAD.last_started_at = _now()
        except Exception as e:
            GPT_LOAD.proc = None
            GPT_LOAD.last_start_error = str(e)

    # Best-effort wait so the first proxied request doesn't race.
    _wait_for_tcp(GPT_LOAD_INTERNAL_HOST, GPT_LOAD_INTERNAL_PORT, timeout_s=10.0)


def _terminate_proc(p: subprocess.Popen[bytes]) -> None:
    try:
        p.terminate()
    except Exception:
        return
    # Give it a moment to exit gracefully; then force kill.
    deadline = _now() + 3.0
    while _now() < deadline:
        try:
            if p.poll() is not None:
                return
        except Exception:
            return
        time.sleep(0.1)
    try:
        p.kill()
    except Exception:
        pass


def _restart_gpt_load(reason: str) -> None:
    """
    Best-effort restart when gpt-load is stuck or database/network glitches happen.
    """
    now = _now()
    with GPT_LOAD.lock:
        if GPT_LOAD.last_restart_at is not None and (now - GPT_LOAD.last_restart_at) < float(GPT_LOAD_RESTART_COOLDOWN_S):
            return
        GPT_LOAD.last_restart_at = now
        if GPT_LOAD.proc is not None and GPT_LOAD.proc.poll() is None:
            old = GPT_LOAD.proc
        else:
            old = None
            if GPT_LOAD.proc is not None:
                try:
                    GPT_LOAD.last_exit_code = int(GPT_LOAD.proc.poll())  # type: ignore[arg-type]
                except Exception:
                    pass
        GPT_LOAD.proc = None

    if old is not None:
        _terminate_proc(old)

    # Append a restart marker to the log.
    try:
        with open("/tmp/gpt-load.log", "ab", buffering=0) as f:
            f.write(f"[hf_server] restarting gpt-load: {reason}\n".encode("utf-8", errors="replace"))
    except Exception:
        pass

    with GPT_LOAD.lock:
        GPT_LOAD.restart_count += 1

    _start_gpt_load_once()


def _probe_gpt_load_once(timeout_s: float = 3.0) -> bool:
    """
    Lightweight probe to detect dead/hung gpt-load.
    """
    try:
        conn = HTTPConnection(GPT_LOAD_INTERNAL_HOST, GPT_LOAD_INTERNAL_PORT, timeout=timeout_s)
        # If auth is enabled, "/" returns the login page and is safe to probe.
        conn.request("GET", "/")
        resp = conn.getresponse()
        _ = resp.read(256)
        ok = 200 <= int(resp.status) < 500
    except Exception:
        ok = False

    if ok:
        with GPT_LOAD.lock:
            GPT_LOAD.last_probe_ok_at = _now()
    return ok


def _gpt_load_watchdog_loop() -> None:
    """
    Keep gpt-load usable on long-running Spaces.

    HF's runtime may experience transient network/db issues; gpt-load may hang or exit.
    """
    # Initial delay so startup logs are readable.
    time.sleep(5.0)
    fail = 0
    while True:
        # Probe periodically; restart after a few consecutive failures.
        ok = _probe_gpt_load_once(timeout_s=3.0)
        if ok:
            fail = 0
        else:
            fail += 1
            if fail >= 3:
                _restart_gpt_load("watchdog probe failed (3x)")
                fail = 0
        time.sleep(20.0)


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
            env = os.environ.copy()
            # Default to the co-located gpt-load instance.
            env.setdefault("GPT_LOAD_BASE_URL", GPT_LOAD_INTERNAL_BASE)
            p = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, env=env)
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

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _send(self, code: int, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _log_page_html(self) -> bytes:
        # A lightweight UI to trigger the generator job and view the tail logs.
        return (
            "<!doctype html><html><head><meta charset='utf-8'/>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
            "<title>mykeeta2gptload / log</title>"
            "<style>"
            "body{font-family:ui-monospace,Menlo,Consolas,monospace;padding:24px;max-width:980px}"
            "a{color:#111} .top{display:flex;gap:12px;align-items:center;flex-wrap:wrap}"
            "button{padding:10px 14px;border:1px solid #111;background:#111;color:#fff;cursor:pointer;border-radius:10px}"
            "button.secondary{background:#fff;color:#111}"
            "pre{white-space:pre-wrap;background:#f6f6f6;padding:12px;border-radius:12px;border:1px solid #e6e6e6;}"
            ".hint{color:#555;font-size:12px}"
            "</style></head><body>"
            "<div class='top'>"
            "<h2 style='margin:0'>Key Generator Log</h2>"
            "<a href='/' target='_self'>Open GPT-Load</a>"
            "</div>"
            "<p class='hint'>HF Spaces: '/' is proxied to GPT-Load. This page is the generator runner.</p>"
            "<p><button onclick='run()'>Run Job</button> "
            "<button class='secondary' onclick='refresh()'>Refresh</button></p>"
            "<pre id='out'>Loading...</pre>"
            "<script>"
            "async function refresh(){"
            " const r=await fetch('/status',{cache:'no-store'}); const j=await r.json();"
            " document.getElementById('out').textContent=JSON.stringify(j,null,2);"
            "}"
            "async function run(){"
            " const r=await fetch('/run',{method:'POST'}); const j=await r.json();"
            " await refresh();"
            "}"
            "refresh();"
            "setInterval(refresh, 5000);"
            "</script></body></html>"
        ).encode("utf-8")

    def _send_log_page(self) -> None:
        self._send(200, self._log_page_html(), "text/html; charset=utf-8")

    def _is_reserved_path(self, path: str) -> bool:
        p = urllib.parse.urlparse(path).path
        return p in ("/health", "/status", "/run", "/log")

    def _proxy_to_gpt_load(self) -> None:
        _start_gpt_load_once()

        try:
            # If gpt-load is still booting, don't thrash with restarts.
            with GPT_LOAD.lock:
                started_at = GPT_LOAD.last_started_at
                restart_count = GPT_LOAD.restart_count
                last_probe_ok_at = GPT_LOAD.last_probe_ok_at
            if not _wait_for_tcp(GPT_LOAD_INTERNAL_HOST, GPT_LOAD_INTERNAL_PORT, timeout_s=2.0):
                # If it's within the startup grace window, return a friendly 503.
                if started_at is not None and (_now() - started_at) < float(GPT_LOAD_STARTUP_GRACE_S):
                    self._send_json(
                        503,
                        {
                            "error": "gpt-load starting",
                            "detail": "gpt-load port not ready yet",
                            "gpt_load_restart_count": restart_count,
                            "gpt_load_last_probe_ok_at": last_probe_ok_at,
                        },
                    )
                    return

            url = urllib.parse.urlparse(self.path)
            target_path = url.path or "/"
            if url.query:
                target_path += "?" + url.query

            body = self._read_body()
            conn = HTTPConnection(GPT_LOAD_INTERNAL_HOST, GPT_LOAD_INTERNAL_PORT, timeout=10)

            # Forward headers (minus hop-by-hop ones).
            hop_by_hop = {
                "connection",
                "keep-alive",
                "proxy-authenticate",
                "proxy-authorization",
                "te",
                "trailers",
                "transfer-encoding",
                "upgrade",
            }
            fwd_headers: dict[str, str] = {}
            for k, v in self.headers.items():
                if k.lower() in hop_by_hop:
                    continue
                # We terminate at this server, so make Host match the internal service.
                if k.lower() == "host":
                    continue
                fwd_headers[k] = v

            fwd_headers["Host"] = f"{GPT_LOAD_INTERNAL_HOST}:{GPT_LOAD_INTERNAL_PORT}"
            if body and "Content-Length" not in fwd_headers:
                fwd_headers["Content-Length"] = str(len(body))

            conn.request(self.command, target_path, body=body if body else None, headers=fwd_headers)
            resp = conn.getresponse()
            resp_body = resp.read()

            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in hop_by_hop:
                    continue
                # We'll re-set Content-Length after reading.
                if k.lower() == "content-length":
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            with GPT_LOAD.lock:
                start_err = GPT_LOAD.last_start_error
                pid = GPT_LOAD.proc.pid if (GPT_LOAD.proc is not None) else None
                running = bool(GPT_LOAD.proc is not None and GPT_LOAD.proc.poll() is None)
                last_probe_ok_at = GPT_LOAD.last_probe_ok_at
                restart_count = GPT_LOAD.restart_count
                started_at = GPT_LOAD.last_started_at
            payload = {
                "error": "gpt-load proxy failed",
                "detail": str(e),
                "gpt_load_running": running,
                "gpt_load_pid": pid,
                "gpt_load_start_error": start_err,
                "gpt_load_last_probe_ok_at": last_probe_ok_at,
                "gpt_load_restart_count": restart_count,
                "gpt_load_log_tail": _tail_text("/tmp/gpt-load.log", max_bytes=8_000)[-8_000:],
                "hint": "Set HF Space Secret GPT_LOAD_AUTH_KEY and (recommended) GPT_LOAD_DATABASE_DSN.",
            }
            # Restart only if we're well past startup grace; avoid restart storms.
            if started_at is None or (_now() - started_at) >= float(GPT_LOAD_STARTUP_GRACE_S):
                threading.Thread(target=_restart_gpt_load, args=(f"proxy error: {e}",), daemon=True).start()
            self._send_json(502, payload)

    def do_HEAD(self) -> None:  # noqa: N802
        # Treat HEAD as a proxy request. This helps with some platform health checks.
        if not self._is_reserved_path(self.path):
            self._proxy_to_gpt_load()
            return
        self._send(404, b"not found\n")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/log" or self.path.startswith("/log?"):
            self._send_log_page()
            return

        if self.path == "/health":
            self._send(200, b"ok\n")
            return

        if self.path == "/status":
            # While a job is running, show a live tail of the current log file.
            live_tail = _tail_text("/tmp/job.log", max_bytes=24_000)[-12_000:]
            with STATE.lock:
                payload = {
                    "running": STATE.running,
                    "last_exit_code": STATE.last_exit_code,
                    "last_started_at": STATE.last_started_at,
                    "last_finished_at": STATE.last_finished_at,
                    "log_tail": (live_tail or STATE.last_log_tail)[-12_000:],
                }
            with GPT_LOAD.lock:
                db_summary = _summarize_database_dsn(
                    (os.getenv("GPT_LOAD_DATABASE_DSN") or os.getenv("DATABASE_DSN") or "").strip()
                )
                # Useful for diagnosing restart storms / persistence issues.
                started_at = GPT_LOAD.last_started_at
                uptime_s = (_now() - started_at) if started_at is not None else None
                payload.update(
                    {
                        "gpt_load_running": bool(GPT_LOAD.proc is not None and GPT_LOAD.proc.poll() is None),
                        "gpt_load_pid": GPT_LOAD.proc.pid if GPT_LOAD.proc is not None else None,
                        "gpt_load_start_error": GPT_LOAD.last_start_error,
                        "gpt_load_restart_count": GPT_LOAD.restart_count,
                        "gpt_load_last_probe_ok_at": GPT_LOAD.last_probe_ok_at,
                        "gpt_load_started_at": started_at,
                        "gpt_load_uptime_s": uptime_s,
                        "gpt_load_exit_code": GPT_LOAD.last_exit_code,
                        "gpt_load_db_mode": db_summary["mode"],
                        "gpt_load_db_host": db_summary["host"],
                        "gpt_load_db_name": db_summary["db"],
                        "gpt_load_log_tail": _tail_text("/tmp/gpt-load.log", max_bytes=8_000)[-8_000:],
                    }
                )
            self._send_json(200, payload)
            return

        # Default: serve GPT-Load management UI to the outside world.
        if not self._is_reserved_path(self.path):
            self._proxy_to_gpt_load()
            return

        self._send(404, b"not found\n")

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/run":
            with STATE.lock:
                if STATE.running:
                    self._send_json(200, {"started": False, "reason": "already_running"})
                    return
                started = _maybe_start_job()
                self._send_json(200, {"started": bool(started)})
            return

        if not self._is_reserved_path(self.path):
            self._proxy_to_gpt_load()
            return

        self._send(404, b"not found\n")

    def do_PUT(self) -> None:  # noqa: N802
        if not self._is_reserved_path(self.path):
            self._proxy_to_gpt_load()
            return
        self._send(404, b"not found\n")

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._is_reserved_path(self.path):
            self._proxy_to_gpt_load()
            return
        self._send(404, b"not found\n")

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._is_reserved_path(self.path):
            self._proxy_to_gpt_load()
            return
        self._send(204, b"")


def main() -> int:
    port = int(os.getenv("PORT", "7860"))
    host = "0.0.0.0"

    # Optional: periodic runner for "always on" Spaces.
    threading.Thread(target=_scheduler_loop, daemon=True).start()

    # Start gpt-load in the background so '/' is immediately usable.
    threading.Thread(target=_start_gpt_load_once, daemon=True).start()
    threading.Thread(target=_gpt_load_watchdog_loop, daemon=True).start()

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"[hf_server] listening on http://{host}:{port}", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
