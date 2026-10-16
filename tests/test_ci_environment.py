"""CI 环境不得继承真实凭据、外部地址或追踪配置。"""
from scripts.ci.offline_env import isolated_env
from scripts.ci.mock_smoke import frames
from tests.redis_test_helpers import redis_endpoint


def test_isolated_env_drops_host_secrets_and_real_services(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_KEY", "synthetic-host-secret")
    monkeypatch.setenv("MODEL_MASTER_KEY", "synthetic-host-master")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "synthetic-host-trace")
    monkeypatch.setenv("HTTPS_PROXY", "http://invalid.local")
    monkeypatch.setenv("REDIS_URL", "redis://invalid.local")
    env = isolated_env(tmp_path)
    assert env["LLM_BASE_URL"] == "http://127.0.0.1:1/v1"
    assert env["LLM_API_KEY"] == "ci-local-placeholder"
    assert env["REDIS_URL"] == ""
    assert env["DATA_DIR"] == str(tmp_path)
    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert not {"MODEL_MASTER_KEY", "OTEL_EXPORTER_OTLP_HEADERS", "HTTPS_PROXY"} & env.keys()


def test_sse_frame_reader_preserves_ids_and_ignores_heartbeats():
    assert frames(': keep-alive\r\n\r\nid: run:1\r\ndata: {"type":"RUN_STARTED"}\r\n\r\n') == [
        {"id": "run:1", "data": {"type": "RUN_STARTED"}}]


def test_test_redis_tcp_endpoint_is_only_loopback(monkeypatch, tmp_path):
    monkeypatch.setenv("VOYACART_REDIS_TEST_TRANSPORT", "tcp")
    args, url, options = redis_endpoint(tmp_path)
    assert args[:2] == ["--bind", "127.0.0.1"]
    assert url.startswith("redis://127.0.0.1:") and options["host"] == "127.0.0.1"


def test_backend_rejects_unexpected_skips(tmp_path):
    from scripts.ci.backend import unexpected_skips
    report = tmp_path / "pytest.xml"
    report.write_text('<testsuites><testsuite><testcase classname="tests.test_agui_loadtest" name="test_locust_interrupted_stream_is_written_to_csv[deadline]"><skipped/></testcase><testcase classname="tests.test_queue" name="test_redis"><skipped/></testcase></testsuite></testsuites>')
    assert unexpected_skips(report) == ["tests.test_queue::test_redis"]
