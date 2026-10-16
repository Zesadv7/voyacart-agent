"""离线、保守的已跟踪文件凭据扫描；报告只含位置和规则，不回显命中内容。"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
RULES = {
    "private_key": re.compile(r"^-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----$", re.M),
    "provider_key": re.compile(r"\b(?:sk-proj-|sk-ant-api\d+-)[A-Za-z0-9_-]{40,}\b"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
}


def scan_text(text: str) -> list[dict]:
    """只匹配高置信度已知格式；不声称能识别任意密码或未知供应商令牌。"""
    findings = []
    for rule, pattern in RULES.items():
        for match in pattern.finditer(text):
            findings.append({"rule": rule, "line": text.count("\n", 0, match.start()) + 1})
    return sorted(findings, key=lambda item: (item["line"], item["rule"]))


def scan_paths(paths: list[Path]) -> list[dict]:
    findings = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        raw = path.read_bytes()
        if b"\x00" in raw:
            continue
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        label = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else path.name
        findings.extend({"path": label, **finding} for finding in scan_text(source))
        # Vite 将 VITE_* 编入公开 JS；只禁止源码读取，检查器/测试描述本身不算读取。
        if path.is_relative_to(ROOT / "frontend/src"):
            token = "VITE_" + "API_TOKEN"
            if re.search(r"import\.meta\.env(?:\." + token + r"|\[['\"]" + token + r"['\"]\])", source):
                findings.append({"path": label, "line": 1, "rule": "compiled_shared_api_token"})
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", nargs="*", type=Path, help="测试用显式文件；默认只扫描 Git 已跟踪文件")
    args = parser.parse_args()
    paths = args.paths
    if paths is None:
        names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\x00")
        paths = [ROOT / name for name in names if name]
    findings = scan_paths([path.resolve() for path in paths])
    for item in findings:
        print(f"{item['path']}:{item['line']}: {item['rule']}")
    print(f"Secret scan: {len(paths)} paths, {len(findings)} findings. Known-format offline check only.")
    raise SystemExit(1 if findings else 0)


if __name__ == "__main__":
    main()
