"""隔离数据目录执行完整 pytest；Locust 必须在独立解释器验收。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from scripts.ci.offline_env import isolated_env


def unexpected_skips(junit: Path) -> list[str]:
    return [case.get("classname", "") + "::" + case.get("name", "")
            for case in ET.parse(junit).getroot().iter("testcase")
            if case.find("skipped") is not None
            and not (case.get("classname") == "tests.test_agui_loadtest"
                     and case.get("name", "").startswith("test_locust_interrupted_stream_is_written_to_csv["))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junitxml")
    args = parser.parse_args()
    # 此入口针对干净 checkout；拒绝加载用户 .env，但保留临时 .env 解析合同测试。
    if (Path(__file__).resolve().parents[2] / ".env").exists():
        raise SystemExit("离线全回归需要没有 .env 的干净 checkout；不要删除真实配置，请另建测试 checkout。")
    with tempfile.TemporaryDirectory(prefix="voyacart-backend-") as directory:
        env = isolated_env(Path(directory))
        env.pop("PYTHON_DOTENV_DISABLED")
        env.pop("OTEL_SDK_DISABLED")
        # 独立官方 Redis 二进制由调用者提供，只传路径。
        env["VOYACART_REDIS_TEST_TRANSPORT"] = os.environ.get("VOYACART_REDIS_TEST_TRANSPORT", "tcp")
        if os.environ.get("VOYACART_REDIS_SERVER_BIN"):
            env["VOYACART_REDIS_SERVER_BIN"] = os.environ["VOYACART_REDIS_SERVER_BIN"]
        command = [sys.executable, "-m", "pytest", "-q", "-ra", "tests", "scripts/esci"]
        junit = Path(args.junitxml).resolve() if args.junitxml else Path(directory) / "pytest.xml"
        command.append("--junitxml=" + str(junit))
        code = subprocess.call(command, env=env)
        if junit.exists():
            skipped = unexpected_skips(junit)
            if skipped:
                print("未授权跳过（不能作为完整回归通过）：" + ", ".join(skipped))
                code = code or 1
        else:
            code = code or 1
        raise SystemExit(code)


if __name__ == "__main__":
    main()
