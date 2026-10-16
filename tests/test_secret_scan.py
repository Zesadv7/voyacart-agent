"""凭据扫描只输出规则/位置，绝不将假秘密再次泄漏到证据。"""
from pathlib import Path
import subprocess
import sys

from scripts.ci.secret_scan import scan_text


def test_known_formats_detected_with_line_number_without_value():
    key = "sk-proj-" + "X" * 50
    result = scan_text("safe\n" + key)
    assert result == [{"rule": "provider_key", "line": 2}]
    assert key not in str(result)


def test_common_safe_values_are_not_credential_findings():
    assert scan_text("ci-local-placeholder\nhttp://mock-llm:8081/v1\nsk-example\na" * 64) == []


def test_scan_cli_failure_does_not_echo_secret(tmp_path):
    value = "ghp_" + "X" * 36
    path = tmp_path / "synthetic.txt"
    path.write_text(value)
    result = subprocess.run([sys.executable, "-m", "scripts.ci.secret_scan", "--paths", str(path)],
        capture_output=True, text=True)
    assert result.returncode == 1
    assert value not in result.stdout + result.stderr
    assert "synthetic.txt:1: github_token" in result.stdout
