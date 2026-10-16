"""M5 观察驱动的纯函数合同；服务合同另见真实进程证据。"""
from scripts.ci.bounded_recovery_probe import body, frames, percentile, supported


def test_synthetic_request_keys_are_stable_and_distinct():
    assert body("r") == body("r")
    assert body("r")["threadId"] != body("s")["threadId"]
    assert body("r")["forwardedProps"] == {"buyerId": "probe-buyer"}


def test_frames_preserve_sequence_and_ignore_keepalive():
    raw = ': keepalive\r\n\r\nid: r:1\r\ndata: {"type":"RUN_STARTED"}\r\n\r\nid: r:2\r\ndata: {"type":"RUN_FINISHED"}\r\n\r\n'
    assert frames(raw) == [{"id":"r:1","event":{"type":"RUN_STARTED"}}, {"id":"r:2","event":{"type":"RUN_FINISHED"}}]


def test_source_constructor_options_do_not_invent_baseline_support():
    class Baseline:
        def __init__(self, journal, queue=None): pass
    assert supported(Baseline, {"queue":1,"max_active_runs":2}) == {"queue":1}


def test_percentiles_are_finite_or_explicitly_missing():
    assert percentile([], .95) is None
    assert percentile([3,1,2], .5) == 2
    assert percentile([1], .99) == 1
