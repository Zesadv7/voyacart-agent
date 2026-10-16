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


@pytest.mark.parametrize('path', ['tests/test_historical_ordinal_binding.py', 'tests/test_reference_regression.py'])
def test_historical_ordinal_guard_is_in_safety_suites_and_model_impact(path):
    from pathlib import Path
    import json
    assert classify([path])['model']
    for name in ('suite.json', 'glm-5.3-flash-suite.json', 'glm-5.3-flash-coding-plan-suite.json'):
        suite = json.loads((Path('eval/harness/v1') / name).read_text())
        assert suite['contract_tests'].count(path) == 1


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


def test_model_job_installs_redis_before_paid_requests_for_full_profiles_only():
    from pathlib import Path
    import yaml
    workflow = yaml.safe_load(Path('.github/workflows/harness-eval.yml').read_text())
    job = workflow['jobs']['model-evaluation']
    steps = job['steps']
    install = next(i for i, step in enumerate(steps) if 'apt-get install -y redis-server' in step.get('run', ''))
    preflight = next(i for i, step in enumerate(steps) if 'scripts.model_preflight' in step.get('run', ''))
    run = next(i for i, step in enumerate(steps) if step.get('name') == 'Real model evaluation')
    assert install < preflight < run
    assert steps[install]['if'] == "inputs.profile != 'preflight'"
    assert 'continue-on-error' not in steps[install]
    assert '--skip-contracts' not in steps[run]['run']
    assert 'safety-contracts' in job['needs']
    assert 'approve_model_budget' in job['if']


def test_full_functional_workflow_is_sequential_bounded_and_separate_from_release():
    from pathlib import Path
    import yaml
    workflow=yaml.safe_load(Path('.github/workflows/harness-eval.yml').read_text())
    events=workflow.get('on',workflow.get(True))
    assert 'full-functional' in events['workflow_dispatch']['inputs']['profile']['options']
    assert '${{ github.event_name }}' in workflow['concurrency']['group']
    assert "inputs.profile == 'config-check' && 'configuration' || 'paid'" in workflow['concurrency']['group']
    assert workflow['concurrency']['cancel-in-progress'] is False
    first=workflow['jobs']['model-evaluation']
    run=next(s for s in first['steps'] if s.get('name')=='Real model evaluation')
    assert "inputs.profile == 'full-functional' && 'dev'" in run['env']['HARNESS_PROFILE']
    assert 'if [ "$HARNESS_PROFILE" = release ]' in run['run']
    assert '--profile release --output eval/verification/ci-model --require-benefit' in run['run']
    check=next(i for i,s in enumerate(first['steps']) if s.get('name')=='Validate full-functional development stage')
    upload=next(i for i,s in enumerate(first['steps']) if s.get('name')=='Preserve HTML and raw failures')
    assert check<upload and first['steps'][check]['if']=="inputs.profile == 'full-functional'"
    last=workflow['jobs']['full-functional-holdout']
    assert last['needs']=='model-evaluation'
    assert 'workflow_dispatch' in last['if'] and 'approve_model_budget' in last['if'] and 'always()' not in last['if']
    assert last['timeout-minutes']==180 and last['env']['LLM_FALLBACK_MODEL']==''
    steps=last['steps']
    download=next(i for i,s in enumerate(steps) if s.get('uses')=='actions/download-artifact@v4')
    assert steps[download]['with']['name']=='harness-model-${{ github.run_id }}-${{ github.run_attempt }}'
    assert 'github-token' not in steps[download]['with'] and 'run-id' not in steps[download]['with']
    verify=next(i for i,s in enumerate(steps) if s.get('name')=='Verify prior stage before any holdout model request')
    evaluate=next(i for i,s in enumerate(steps) if s.get('name')=='Complete frozen holdout functionality')
    redis=next(i for i,s in enumerate(steps) if 'apt-get install -y redis-server' in s.get('run',''))
    assert download<verify<evaluate and redis<evaluate
    assert '--profile release' in steps[evaluate]['run'] and '--require-benefit' not in steps[evaluate]['run']
    assert '--skip-contracts' not in steps[evaluate]['run']
    assert not any('scripts.model_preflight' in s.get('run','') for s in steps)
    assert not any('continue-on-error' in s for s in steps)
    assert any('--holdout eval/verification/ci-model' in s.get('run','') and s.get('if')=='always()' for s in steps)
