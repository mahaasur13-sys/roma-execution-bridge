import logging

import main


def _fmt(*args):
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args, None)
    main._RedactQueryFilter().filter(rec)
    return rec.getMessage()


def test_token_redacted():
    out = _fmt("1.2.3.4:5", "GET", "/auth/verify-email?token=SECRETVALUE123", "1.1", 200)
    assert "SECRETVALUE123" not in out and "token=***" in out


def test_other_params_kept():
    out = _fmt("1.2.3.4:5", "GET", "/jobs?page=2&token=abc&x=1", "1.1", 200)
    assert "page=2" in out and "x=1" in out and "abc" not in out


def test_filter_installed():
    assert any(isinstance(f, main._RedactQueryFilter) for f in logging.getLogger("uvicorn.access").filters)
