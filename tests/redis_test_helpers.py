"""测试专用 Redis 地址；只对刚启动的隔离进程操作。"""
import os
from pathlib import Path
import socket



def redis_endpoint(directory: str | Path) -> tuple[list[str], str, dict]:
    # 受限容器可能不支持 AF_UNIX；显式使用回环 TCP，不连接外部 Redis。
    transport = os.getenv("VOYACART_REDIS_TEST_TRANSPORT", "unix")
    if transport == "tcp":
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        return ["--bind", "127.0.0.1", "--port", str(port)], f"redis://127.0.0.1:{port}/0", {"host": "127.0.0.1", "port": port}
    if transport != "unix":
        raise ValueError("VOYACART_REDIS_TEST_TRANSPORT 只支持 unix / tcp")
    path = Path(directory) / "redis.sock"
    return ["--port", "0", "--unixsocket", str(path), "--unixsocketperm", "700"], f"unix://{path}", {"unix_socket_path": str(path)}
