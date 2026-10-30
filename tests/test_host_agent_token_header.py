"""--token-header: default Authorization unchanged; x-roma-host-token for fronts that strip Authorization."""

from __future__ import annotations

import json
import logging

import pytest

from host_agent import agent as ag

TOK = "tok-SECRET-value-123"


def _capture():
    calls = []

    def post(url, headers, data, timeout):
        calls.append((url, dict(headers), data))
        return 204, b""

    return calls, post


def test_default_sends_authorization_bearer_byte_for_byte():
    calls, post = _capture()
    send = ag.http_transport("https://roma.invalid/hosts/agent/", post=post)
    send({"action": "heartbeat", "host_id": "h1", "token": TOK, "x": 1})
    url, headers, data = calls[0]
    assert url == "https://roma.invalid/hosts/agent/heartbeat"
    assert headers == {"Content-Type": "application/json", "Authorization": f"Bearer {TOK}", "X-Roma-Host-Id": "h1"}
    assert list(headers) == ["Content-Type", "Authorization", "X-Roma-Host-Id"]
    assert data == json.dumps({"x": 1}).encode()
    assert not any(k.lower() == "x-roma-host-token" for k in headers)


def test_x_roma_host_token_mode_sends_only_custom_header():
    calls, post = _capture()
    send = ag.http_transport("https://roma.invalid/hosts/agent", post=post, token_header="x-roma-host-token")
    for action in ("heartbeat", "fetch_job", "submit_result"):
        send({"action": action, "host_id": "h1", "token": TOK})
    for _url, headers, data in calls:
        assert headers["X-Roma-Host-Token"] == TOK and headers["X-Roma-Host-Id"] == "h1"
        assert not any(k.lower() == "authorization" for k in headers)
        assert TOK.encode() not in data
    assert [c[0].rsplit("/", 1)[1] for c in calls] == ["heartbeat", "job", "result"]


def test_invalid_choice_rejected(capsys):
    with pytest.raises(ValueError):
        ag.http_transport("https://x", token_header="cookie")
    with pytest.raises(SystemExit):
        ag.main(["--token-header", "cookie"])
    with pytest.raises(SystemExit):
        ag.load_config(
            None, env={"ROMA_URL": "u", "ROMA_HOST_ID": "h", "ROMA_HOST_TOKEN": TOK, "ROMA_TOKEN_HEADER": "bogus"}
        )
    assert TOK not in capsys.readouterr().err


def test_env_and_cli_select_mode():
    base = {"ROMA_URL": "u", "ROMA_HOST_ID": "h", "ROMA_HOST_TOKEN": TOK}
    assert ag.load_config(None, env=base).token_header == "authorization"
    cfg = ag.load_config(None, env={**base, "ROMA_TOKEN_HEADER": "x-roma-host-token"})
    assert cfg.token_header == "x-roma-host-token" and TOK not in repr(cfg)


@pytest.mark.parametrize("mode", ["authorization", "x-roma-host-token"])
def test_token_absent_from_logs_and_dry_run_output(mode, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    seen = []

    def post(url, headers, data, timeout):
        seen.append(headers)
        if url.endswith("/job"):
            job = {"type": "verify", "job_id": "j1", "job_token": "jt", "seed": "s", "n": 8, "reps": 1}
            return 200, json.dumps({"job": job}).encode()
        return 200, b'{"ok": true}'

    monkeypatch.setattr(ag, "_urllib_post", post)
    for k in ("ROMA_HOST_TOKEN_FILE", "ROMA_TOKEN_HEADER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ROMA_URL", "https://roma.invalid/hosts/agent")
    monkeypatch.setenv("ROMA_HOST_ID", "h1")
    monkeypatch.setenv("ROMA_HOST_TOKEN", TOK)
    assert ag.main(["--dry-run", "--token-header", mode]) == 0
    out = capsys.readouterr()
    assert seen, "agent made no request"
    key = "Authorization" if mode == "authorization" else "X-Roma-Host-Token"
    assert all(key in h for h in seen)
    assert TOK not in out.out + out.err + caplog.text
