"""Hosted demo entrypoint: one process serves the API and the built UI on 0.0.0.0:$PORT.

  python -m backend.hosted

Environment (names only; values are never printed):
  PORT                      listen port (default 8080)
  SETTLEWELL_MODE           mock (default) or sandbox
  SETTLEWELL_DB             SQLite path (default /data/settlewell.db, else /tmp/settlewell/...)
  SETTLEWELL_DEMO_PASSCODE  passcode gate for every page and route except /health
  SETTLEWELL_SESSION_SECRET optional cookie-signing secret (random per process if unset)
  SETTLEWELL_PUBLIC_ORIGIN  e.g. https://settlewell.up.railway.app (RAILWAY_PUBLIC_DOMAIN is
                            picked up automatically)
  SETTLEWELL_ALLOWED_HOSTS  extra exact Host names, comma separated
  SETTLEWELL_STATIC_DIR     built UI (default frontend/dist)
  SETTLEWELL_RESET_ON_BOOT  mock: reseed INV-2291 on every boot (default 1)
  SETTLEWELL_SANDBOX_AUTORUN sandbox: run the live sandbox hero on boot when no run exists, and
                            allow the console's "Reset demo" to rerun it (default 0)
  AIRWALLEX_SANDBOX_CLIENT_ID, AIRWALLEX_SANDBOX_API_KEY, ANTHROPIC_API_KEY, ANTHROPIC_MODEL,
  AIRWALLEX_WEBHOOK_SECRET  sandbox mode only

Sandbox mode is refused without a passcode: a public link must not spend sandbox calls or
model credits for anyone who finds it.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

RERUN_MIN_INTERVAL_S = 120


def default_db(env=None) -> str:
    source = os.environ if env is None else env
    if source.get("SETTLEWELL_DB"):
        return source["SETTLEWELL_DB"]
    data = Path("/data")
    if data.is_dir() and os.access(data, os.W_OK):
        return str(data / "settlewell.db")
    return "/tmp/settlewell/settlewell.db"


class SandboxDemo:
    """Runs the live sandbox hero (and its owned-only reset) in one background thread at a time.

    Every money step inside the hero, and every SCHEDULED cancel in the reset, goes through
    execute() with a bound single-use approval (backend.hero_live / backend.scripts.hero_run)."""

    def __init__(self, manifest_path: str, db_path: str, *, runner=None, resetter=None,
                 clock=time.monotonic) -> None:
        self.manifest_path, self.db_path = manifest_path, db_path
        self._runner = runner or self._run_hero
        self._resetter = resetter or self._reset_owned
        self._clock = clock
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_start: float | None = None
        self._status: dict = {"state": "idle", "beats": {}, "problems": [],
                              "started_at": None, "finished_at": None}

    def status(self) -> dict:
        with self._lock:
            return {**self._status, "beats": dict(self._status["beats"]),
                    "problems": list(self._status["problems"])}

    def start(self, *, reset: bool) -> tuple[bool, str]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False, "a sandbox run is already in progress"
            now = self._clock()
            if self._last_start is not None and now - self._last_start < RERUN_MIN_INTERVAL_S:
                return False, f"wait {RERUN_MIN_INTERVAL_S}s between sandbox reruns"
            self._last_start = now
            self._status = {"state": "running", "beats": {}, "problems": [],
                            "started_at": datetime.now(UTC).isoformat(), "finished_at": None}
            self._thread = threading.Thread(target=self._work, args=(reset,), daemon=True,
                                            name="settlewell-sandbox-demo")
            self._thread.start()
            return True, ""

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _work(self, reset: bool) -> None:
        state, beats, problems = "failed", {}, []
        print("sandbox demo: run started, reset", reset, flush=True)
        try:
            if reset and Path(self.manifest_path).exists():
                ok, problems = self._resetter()
                if not ok:
                    raise RuntimeError("reset_refused")
            beats = self._runner()
            state = "done" if all(str(v).startswith(("PASS", "PARTIAL")) for v in beats.values()) \
                else "failed"
        except Exception as error:  # noqa: BLE001 - sanitized: type name only, never a response
            problems = [*problems, type(error).__name__]
        # beats and problems are sanitized (status words, ids, error type names), never a response
        print("sandbox demo: run", state, "beats", beats, "problems", problems, flush=True)
        with self._lock:
            self._status.update(state=state, beats=dict(beats), problems=list(problems),
                                finished_at=datetime.now(UTC).isoformat())

    def _run_hero(self) -> dict[str, str]:
        from backend.scripts.hero_run import run_sandbox

        return run_sandbox(self.manifest_path, db=self.db_path)

    def _reset_owned(self) -> tuple[bool, list[str]]:
        from backend.api_mode import make_client_factory, resolve_mode
        from backend.sandbox import SandboxDriver
        from backend.scripts.hero_run import reset_owned

        config = resolve_mode("sandbox")
        if config.mode != "SANDBOX":
            return False, [config.reason]
        client = make_client_factory(config)(None)
        return reset_owned(self.manifest_path, client, SandboxDriver(client, confirm_sandbox=True))


def build(env=None):
    """Return (app, port). Raises SystemExit with a sanitized message on bad configuration."""
    source = os.environ if env is None else env
    from backend.api import create_app, reset_mock_demo
    from backend.api_mode import make_client_factory, resolve_mode
    from backend.hosting import Hosting

    requested = (source.get("SETTLEWELL_MODE") or "mock").strip().lower()
    if requested not in ("mock", "sandbox"):
        raise SystemExit("refused: SETTLEWELL_MODE must be mock or sandbox")
    port = int(source.get("PORT") or 8080)
    if not 1 <= port <= 65535:
        raise SystemExit("refused: PORT must be 1-65535")
    root = Path(__file__).resolve().parent.parent
    try:
        hosting = Hosting.from_env(source, static_dir=source.get("SETTLEWELL_STATIC_DIR")
                                   or str(root / "frontend" / "dist"))
    except ValueError as error:
        raise SystemExit(f"refused: {error}") from None
    config = resolve_mode(requested, source)
    if requested == "sandbox" and not hosting.gate_enabled:
        raise SystemExit("refused: sandbox mode needs SETTLEWELL_DEMO_PASSCODE when hosted")
    if config.mode == "MOCK":
        os.environ["SETTLEWELL_MOCK"] = "1"
    else:
        os.environ.pop("SETTLEWELL_MOCK", None)
    # Hosted demo defaults (an explicit host env value always wins): risk-tiered autonomy on,
    # auto limit EUR 5,000 so the EUR 4,200 scheduled duplicate is cancel-eligible. The code
    # default elsewhere is autonomy OFF and EUR 1,000. SETTLEWELL_AUTONOMY=0 restores human-only.
    for key, default in (("SETTLEWELL_AUTONOMY", "1"), ("SETTLEWELL_AUTO_MAX_AMOUNT", "5000")):
        os.environ[key] = source.get(key) or default
    db = default_db(source)
    sandbox_demo = None
    if requested == "sandbox":
        data_dir = Path(db).resolve().parent
        data_dir.mkdir(parents=True, exist_ok=True)
        manifest = str(data_dir / "hero-run-manifest.json")
        from backend.hero_live import load_manifest

        db = load_manifest(manifest).get("db") or str(data_dir / "hero-sandbox.db")
        if not Path(db).exists():  # empty schema, so the console lists nothing instead of erroring
            from backend.ledger import connect, migrate

            conn = connect(db)
            try:
                migrate(conn)
            finally:
                conn.close()
        if config.mode == "SANDBOX" and source.get("SETTLEWELL_SANDBOX_AUTORUN") == "1":
            sandbox_demo = SandboxDemo(manifest, db)
            if not Path(manifest).exists():
                sandbox_demo.start(reset=False)
    app = create_app(db, port=port, worker=config.writes_enabled, mode_config=config,
                     client_factory=make_client_factory(config), hosting=hosting,
                     sandbox_demo=sandbox_demo)
    if config.mode == "MOCK" and source.get("SETTLEWELL_RESET_ON_BOOT", "1") == "1":
        reset_mock_demo(db)
    print("mode", config.mode, "writes", config.writes_enabled, config.reason,
          ",".join(config.missing), "gate", hosting.gate_enabled,
          "ui", hosting.static_dir is not None and Path(hosting.static_dir, "index.html").is_file())
    return app, port


def main() -> int:
    import uvicorn

    app, port = build()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning", proxy_headers=True,
                forwarded_allow_ips="*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
