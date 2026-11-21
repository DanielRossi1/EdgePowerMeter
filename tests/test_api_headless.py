"""End-to-end test of `edgepowermeter --headless`: a real process, port 0, SIGTERM."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"),
                                reason="SIGTERM shutdown test is POSIX only")


def _request(url: str, token: str = "", body: dict | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_headless_serves_api_and_exits_cleanly_on_sigterm(tmp_path):
    discovery = tmp_path / "api" / "api.json"
    home = tmp_path / "home"
    env = dict(os.environ)
    env.update({
        "QT_QPA_PLATFORM": "offscreen",
        "EPM_API_DISCOVERY": str(discovery),
        # Keep the user's real settings untouched (QSettings on Linux honours these).
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "HOME": str(home),
    })
    env.pop("EPM_API_URL", None)
    env.pop("EPM_API_TOKEN", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.main", "--headless", "--api-port", "0"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        info = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"headless process exited early: {proc.stderr.read()}")
            try:
                info = json.loads(discovery.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                time.sleep(0.1)
        assert info is not None, "discovery file was not written"
        url, token = info["url"], info["token"]
        assert token and url.startswith("http://127.0.0.1:")

        status, health = _request(url + "/api/v1/health")
        assert status == 200 and health["ok"] is True

        status, body = _request(url + "/api/v1/tools/get_status", token, {})
        assert status == 200, body
        assert body["ok"] is True
        assert body["result"]["running"] is False

        status, body = _request(url + "/api/v1/tools/get_status", "wrong-token", {})
        assert status == 401

        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=20)
        stderr = proc.stderr.read()
        assert code == 0, stderr
        assert "Traceback" not in stderr
        assert not discovery.exists(), "discovery file must be removed on exit"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
