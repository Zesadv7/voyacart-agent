"""CI 分层与 ESCI 新版本覆盖合同，不需要模型。"""
import pytest

from scripts.ci.scope import classify


@pytest.mark.parametrize("path", ["eval/harness/esci-v1/preparation.json", "eval/harness/esci-v2/suite.json",
    "eval/esci-v2/baselines/approved-v2.json", "data/catalog-esci-v2.jsonl.gz", "data/esci/selection-v2.json",
    "scripts/esci/finalize_catalog.py", "configs/models.yaml"])
def test_new_esci_paths_trigger_backend_and_model_scope(path):
    result = classify([path])
    assert result["backend"] and result["model"]


@pytest.mark.parametrize("path,layer", [("frontend/src/App.tsx", "frontend"),
    ("docker/docker-compose.scale.yaml", "compose"), ("configs/models.yaml", "compose"),
    ("app/presentation/confirmations.py", "frontend"), ("tests/test_ci_scope.py", "backend")])
def test_changed_layer_is_checked(path, layer):
    assert classify([path])[layer]


def test_docs_do_not_spend_model_budget():
    assert not any(classify(["README.md", "docs/改动记录/2026-10-16/M1.md"]).values())


def test_ci_changes_and_manual_run_cover_every_offline_layer():
    for path in (".github/workflows/offline-ci.yml", "scripts/ci/scope.py", "Makefile"):
        assert all(classify([path])[layer] for layer in ("backend", "frontend", "compose"))
    assert all(classify([], all_layers=True).values())


def test_safety_contract_job_installs_real_redis_instead_of_allowing_skips():
    from pathlib import Path
    import yaml
    workflow = yaml.safe_load(Path('.github/workflows/harness-eval.yml').read_text())
    steps = workflow['jobs']['safety-contracts']['steps']
    commands = [step.get('run', '') for step in steps]
    install = next(i for i, command in enumerate(commands) if 'apt-get install -y redis-server' in command)
    contracts = next(i for i, command in enumerate(commands) if '--profile contracts' in command)
    assert install < contracts
    assert 'continue-on-error' not in workflow['jobs']['safety-contracts']
