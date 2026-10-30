"""Host agent sends an explicit User-Agent (Cloudflare in front of zo.space 403s Python-urllib)."""

from __future__ import annotations

import pytest

from host_agent import agent as ag


def _capture():
    calls = []

    def post(url, headers, data, timeout):
        calls.append(dict(headers))
        return 204, b""

    return calls, post


@pytest.mark.parametrize("mode", ["authorization", "x-roma-host-token"])
def test_user_agent_on_every_route(mode):
    calls, post = _capture()
    send = ag.http_transport("https://roma.invalid/hosts/agent", post=post, token_header=mode)
    for action in ("heartbeat", "fetch_job", "submit_result"):
        send({"action": action, "host_id": "h1", "token": "t"})
    assert len(calls) == 3
    for h in calls:
        ua = h["User-Agent"]
        assert ua == ag.USER_AGENT
        assert ua.startswith("roma-host-agent/")
        assert "python-urllib" not in ua.lower()
        assert list(h)[-1] == "User-Agent"


def test_user_agent_constant():
    assert ag.USER_AGENT == f"roma-host-agent/{ag.AGENT_VERSION}"


def test_urllib_post_request_carries_user_agent(monkeypatch):
    seen = {}

    class _Resp:
        status = 204

        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        seen["method"] = req.get_method()
        return _Resp()

    monkeypatch.setattr(ag.urllib.request, "urlopen", fake_urlopen)
    ag._urllib_post("https://roma.invalid/x", {"Content-Type": "application/json"}, b"{}", 5)
    assert seen["method"] == "POST"
    assert seen["ua"] == ag.USER_AGENT
    assert "Python-urllib" not in seen["ua"]
