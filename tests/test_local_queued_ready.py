from billing.execution_worker import dispatch_is_ready


def test_local_queued_is_ready():
    assert dispatch_is_ready("local", "queued") is True


def test_vastai_queued_is_not_ready():
    assert dispatch_is_ready("vastai", "queued") is False


def test_running_is_ready():
    assert dispatch_is_ready("vastai", "running") is True


def test_provisioning_is_ready():
    assert dispatch_is_ready("vastai", "provisioning") is True
