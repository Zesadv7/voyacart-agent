"""部署配置合同：真实 Compose 只解析合成配置，不连接 Docker daemon 或模型。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "docker/docker-compose.yaml"
SCALE = ROOT / "docker/docker-compose.scale.yaml"
GUARD_DEFAULTS = {
    "PUBLIC_DEPLOYMENT": "0",
    "CORS_ORIGINS": "http://localhost:5173,http://127.0.0.1:5173",
    "HTTP_MAX_REQUEST_BYTES": "1048576",
    "HTTP_BODY_TIMEOUT_SECONDS": "30",
    "HTTP_REQUESTS_PER_MINUTE": "0",
    "RUN_MAX_ACTIVE": "8",
    "RUN_TIMEOUT_SECONDS": "300",
    "QUEUE_MAX_AGE_SECONDS": "900",
    "AGUI_QUEUE_MAX_PENDING": "200",
}
GUARD_OVERRIDES = {
    "PUBLIC_DEPLOYMENT": "1",
    "CORS_ORIGINS": "https://delivery.example.invalid,https://buyer.example.invalid",
    "HTTP_MAX_REQUEST_BYTES": "2097152",
    "HTTP_BODY_TIMEOUT_SECONDS": "17",
    "HTTP_REQUESTS_PER_MINUTE": "120",
    "RUN_MAX_ACTIVE": "5",
    "RUN_TIMEOUT_SECONDS": "43",
    "QUEUE_MAX_AGE_SECONDS": "79",
    "AGUI_QUEUE_MAX_PENDING": "13",
}
HEALTH_TEST = [
    "CMD", "python", "-c",
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)",
]


class ComposeSourceLoader(yaml.SafeLoader):
    """只为检查源文件识别 reset 标签；合并语义必须另外用官方 Compose 验证。"""


ComposeSourceLoader.add_constructor("!reset", lambda loader, node: loader.construct_sequence(node))


def _source(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=ComposeSourceLoader)


def _compose_command(environment: dict[str, str]) -> list[str]:
    configured = os.environ.get("VOYACART_COMPOSE_BIN")
    if configured:
        if not Path(configured).is_file():
            pytest.fail("VOYACART_COMPOSE_BIN 必须指向现有官方 Compose 二进制")
        return [configured]
    # 常规 Docker 插件优先；独立 Compose 也能在没有 daemon 的环境完成 config。
    docker = shutil.which("docker")
    if docker:
        probe = subprocess.run([docker, "compose", "version"], env=environment, capture_output=True, timeout=10)
        if probe.returncode == 0:
            return [docker, "compose"]
    for name in ("docker-compose", "voyacart-compose"):
        found = shutil.which(name)
        if found:
            return [found]
    pytest.skip("真实 Compose 合同需要 Docker Compose >=2.24.4；设置 VOYACART_COMPOSE_BIN 或 PATH 后重跑")


def _resolved(tmp_path: Path, *, scale: bool, overrides: dict[str, str] | None = None) -> dict:
    # 不复制宿主认证、代理、模型或 Docker context；显式空 env-file 禁止读取仓库 .env。
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    environment.update({
        "HOME": str(tmp_path / "home"),
        "COMPOSE_DISABLE_ENV_FILE": "1",
        "COMPOSE_PROJECT_NAME": "voyacart-delivery-contract",
        "LLM_BASE_URL": "http://mock-llm:8081/v1",
        "LLM_API_KEY": "ci-compose-synthetic-placeholder",
    })
    environment.update(overrides or {})
    env_file = tmp_path / "empty.env"
    env_file.write_text("")
    command = _compose_command(environment) + ["--env-file", str(env_file), "-f", str(SCALE if scale else BASE)]
    if scale:
        command.extend(["--profile", "mock"])
    command.extend(["config", "--format", "json"])
    result = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
    # 只在内存检查完整合成配置；失败不把任何 config 文本写入证据或测试输出。
    assert result.returncode == 0, f"Compose config 失败（exit={result.returncode}），请用隔离合成环境复查"
    return json.loads(result.stdout)


def _ports(service: dict) -> list[tuple[str, str, int]]:
    return [(port.get("host_ip", ""), str(port.get("published", "")), int(port["target"]))
            for port in service.get("ports", [])]


def _assert_limits(service: dict, *, cpus: float, memory: int, pids: int) -> None:
    assert float(service["cpus"]) == cpus
    assert int(service["mem_limit"]) == memory
    assert int(service["pids_limit"]) == pids
    assert service["logging"] == {
        "driver": "json-file", "options": {"max-size": "10m", "max-file": "3"},
    }


def test_source_shared_guards_and_m5_defaults_are_not_overridden() -> None:
    base, scale = _source(BASE), _source(SCALE)
    for key, default in GUARD_DEFAULTS.items():
        expression = "${" + key + ":-" + default + "}"
        assert base["x-agent-options"][key] == expression
        assert all(base["services"][name]["environment"][key] == expression for name in ("app", "worker"))
        assert key not in scale["x-scale-options"]
        assert all(key not in scale["services"][name]["environment"]
                   for name in ("api", "worker", "automation-worker"))


def test_source_private_ports_health_and_scale_reset() -> None:
    base, scale = _source(BASE), _source(SCALE)
    assert base["services"]["app"]["ports"] == ["127.0.0.1:8000:8000"]
    assert base["services"]["frontend"]["ports"] == ["127.0.0.1:5173:80"]
    assert all("ports" not in base["services"][name] for name in ("redis", "qdrant", "worker"))
    # reset 标签必须保留，即使基础文件未来又加端口，scale 也不会意外继承。
    tree = yaml.compose(SCALE.read_text())
    services_node = next(value for key, value in tree.value if key.value == "services")
    for name in ("api", "redis", "qdrant"):
        service = next(value for key, value in services_node.value if key.value == name)
        ports = next(value for key, value in service.value if key.value == "ports")
        assert ports.tag == "!reset"
        assert not ports.value
    assert base["services"]["app"]["healthcheck"]["test"] == HEALTH_TEST
    assert "healthcheck" not in scale["services"]["api"]
    assert base["services"]["frontend"]["depends_on"]["app"]["condition"] == "service_healthy"
    assert scale["services"]["nginx"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert "healthcheck" not in base["services"]["qdrant"]
    assert base["services"]["app"]["depends_on"]["qdrant"]["condition"] == "service_started"


def test_source_redis_identity_is_not_evicted_and_runtime_is_frozen() -> None:
    base = _source(BASE)
    command = base["services"]["redis"]["command"]
    for flag, expected in (("--appendonly", "yes"), ("--appendfsync", "always"),
                           ("--maxmemory", "256mb"), ("--maxmemory-policy", "noeviction")):
        assert command[command.index(flag) + 1] == expected
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "uv:0.12.23" in dockerfile
    assert "uv:latest" not in dockerfile
    assert "uv sync --frozen --no-install-project --no-dev --extra retrieval" in dockerfile
    assert "COPY scripts/ops ./scripts/ops" in dockerfile
    assert "COPY scripts/__init__.py scripts/issue_identity_token.py ./scripts/" in dockerfile
    assert "COPY . " not in dockerfile
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert {".env", ".env.*", ".venv", "data/*", "eval/verification"}.issubset(ignored)
    assert "scripts/ops" not in ignored
    assert all("VITE_API_TOKEN" not in path.read_text() for path in (BASE, SCALE, ROOT / "frontend/Dockerfile"))


@pytest.mark.parametrize("scale", [False, True], ids=["ordinary", "scale"])
@pytest.mark.parametrize("custom", [False, True], ids=["defaults", "custom-guards"])
def test_actual_compose_resolves_private_guards_resources_and_health(tmp_path: Path, scale: bool, custom: bool) -> None:
    overrides = dict(GUARD_OVERRIDES) if custom else {}
    if custom:
        overrides["VOYACART_SCALE_PORT"] = "18080"
    config = _resolved(tmp_path, scale=scale, overrides=overrides)
    services = config["services"]
    expected = GUARD_OVERRIDES if custom else GUARD_DEFAULTS
    agent_names = ("api", "worker", "automation-worker") if scale else ("app", "worker")
    for name in agent_names:
        service = services[name]
        assert {key: service["environment"][key] for key in expected} == expected
        assert service["environment"]["LLM_API_KEY"] == "ci-compose-synthetic-placeholder"
        _assert_limits(service, cpus=2.0, memory=2 * 1024**3, pids=256)
        if name != "app":
            assert _ports(service) == []
    assert _ports(services["redis"]) == _ports(services["qdrant"]) == []
    _assert_limits(services["redis"], cpus=1.0, memory=512 * 1024**2, pids=64)
    _assert_limits(services["qdrant"], cpus=2.0, memory=2 * 1024**3, pids=256)
    api, gateway = ("api", "nginx") if scale else ("app", "frontend")
    assert services[api]["healthcheck"]["test"] == HEALTH_TEST
    assert services[api]["healthcheck"]["interval"] == "5s"
    assert services[api]["healthcheck"]["retries"] == 60
    assert services[gateway]["depends_on"][api]["condition"] == "service_healthy"
    assert services[api]["depends_on"]["redis"]["condition"] == "service_healthy"
    assert services[api]["depends_on"]["qdrant"]["condition"] == "service_started"
    _assert_limits(services[gateway], cpus=0.5, memory=128 * 1024**2, pids=64)
    if scale:
        assert _ports(services["nginx"]) == [("127.0.0.1", "18080" if custom else "8080", 80)]
        assert _ports(services["mock-llm"]) == []
        _assert_limits(services["mock-llm"], cpus=1.0, memory=256 * 1024**2, pids=64)
        for name in agent_names:
            assert services[name]["environment"]["AUTOMATION_WORKER_MODE"] == "external"
            assert services[name]["environment"]["AGUI_QUEUE_ENABLED"] == "1"
            assert services[name]["environment"]["SEQUENCING_PERSISTENCE_ENABLED"] == "1"
    else:
        assert _ports(services["app"]) == [("127.0.0.1", "8000", 8000)]
        assert _ports(services["frontend"]) == [("127.0.0.1", "5173", 80)]
    for service in services.values():
        assert "VITE_API_TOKEN" not in service.get("environment", {})
        assert "VITE_API_TOKEN" not in service.get("build", {}).get("args", {})


@pytest.mark.parametrize("scale", [False, True], ids=["ordinary", "scale"])
def test_actual_compose_preserves_optional_empty_values(tmp_path: Path, scale: bool) -> None:
    empty_names = ("IDENTITY_HMAC_SECRET", "MODEL_MASTER_KEY", "LLM_FALLBACK_MODEL",
                   "LLM_ENABLE_THINKING", "LLM_FALLBACK_CONTEXT_SIZE", "LANGFUSE_SECRET_KEY")
    config = _resolved(tmp_path, scale=scale, overrides=dict.fromkeys(empty_names, ""))
    for name in (("api", "worker", "automation-worker") if scale else ("app", "worker")):
        assert {key: config["services"][name]["environment"][key] for key in empty_names} == dict.fromkeys(empty_names, "")


@pytest.mark.parametrize("scale", [False, True], ids=["ordinary", "scale"])
def test_actual_compose_does_not_inherit_host_or_dotenv_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scale: bool) -> None:
    for key in ("PUBLIC_DEPLOYMENT", "RUN_MAX_ACTIVE", "CORS_ORIGINS", "IDENTITY_HMAC_SECRET", "LANGFUSE_SECRET_KEY"):
        monkeypatch.setenv(key, "synthetic-host-value-must-not-leak")
    (tmp_path / ".env").write_text("PUBLIC_DEPLOYMENT=1\nRUN_MAX_ACTIVE=999\nIDENTITY_HMAC_SECRET=synthetic-dotenv-must-not-leak\n")
    config = _resolved(tmp_path, scale=scale)
    for name in (("api", "worker", "automation-worker") if scale else ("app", "worker")):
        environment = config["services"][name]["environment"]
        assert {key: environment[key] for key in GUARD_DEFAULTS} == GUARD_DEFAULTS
        assert environment["IDENTITY_HMAC_SECRET"] == environment["LANGFUSE_SECRET_KEY"] == ""
