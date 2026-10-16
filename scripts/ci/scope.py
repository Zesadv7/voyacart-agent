"""分层路径合同；新增数据版本不能绕过检索及 Agent 检查。"""
from __future__ import annotations

import argparse
import json
import subprocess


def classify(paths: list[str], *, all_layers: bool = False) -> dict[str, bool]:
    common = (".github/workflows/", "scripts/ci/", "Makefile")
    backend = common + ("app/", "tests/", "scripts/", "configs/", "data/", "eval/harness/",
                        "eval/esci/", "eval/esci-v2/", "eval/learning/", "pyproject.toml", "uv.lock", ".python-version")
    frontend = common + ("frontend/", "app/presentation/", "app/application/agents/", "app/domain/")
    compose = common + ("docker/", "Dockerfile", ".dockerignore", ".env.example", "frontend/Dockerfile",
                       "frontend/nginx.conf", "configs/")
    model = ("app/", "scripts/eval/", "scripts/esci/", "eval/harness/", "eval/esci/", "eval/esci-v2/",
             "eval/learning/", "tests/", "data/catalog", "data/esci/", "configs/", "Makefile", "pyproject.toml", "uv.lock")
    return {name: all_layers or any(path.startswith(prefixes) for path in paths)
            for name, prefixes in (("backend", backend), ("frontend", frontend), ("compose", compose), ("model", model))}


def changed_paths(base: str) -> list[str]:
    # 首次推送没有有效 before；全量执行比静默漏测安全。
    if not base or set(base) == {"0"}:
        return subprocess.check_output(["git", "ls-files"], text=True).splitlines()
    return subprocess.check_output(["git", "diff", "--name-only", base, "HEAD"], text=True).splitlines()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--github-output")
    args = parser.parse_args()
    result = classify(changed_paths(args.base), all_layers=args.all)
    print(json.dumps(result, sort_keys=True))
    if args.github_output:
        with open(args.github_output, "a") as output:
            output.writelines(f"{key}={str(value).lower()}\n" for key, value in result.items())


if __name__ == "__main__":
    main()
