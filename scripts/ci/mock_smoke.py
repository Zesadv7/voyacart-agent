"""真实 HTTP 冷启动、SSE 游标、确认幂等与重启恢复；只连接本机假模型。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx

from scripts.ci.offline_env import isolated_env

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def start(stack: ExitStack, module: str, port: int, env: dict, logfile: Path) -> subprocess.Popen:
    log = stack.enter_context(logfile.open("w"))
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1",
                                "--port", str(port), "--no-access-log", "--app-dir", str(ROOT / "scripts/loadtest" if module == "mock_llm:app" else ROOT)], cwd=ROOT, env=env,
                               stdout=log, stderr=subprocess.STDOUT)
    stack.callback(stop, process)
    deadline = time.monotonic() + 180
    with httpx.Client(trust_env=False, timeout=2) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{module} exited {process.returncode}; see {logfile.name}")
            try:
                response = client.get(f"http://127.0.0.1:{port}/health")
                if response.status_code == 200 and response.json().get("status") == "ok":
                    return process
            except httpx.HTTPError:
                pass
            time.sleep(.1)
    raise TimeoutError(f"{module} startup timeout; see {logfile.name}")


def frames(text: str) -> list[dict]:
    result = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        event = {}
        for line in block.splitlines():
            if line.startswith("id:"):
                event["id"] = line[3:].strip()
            elif line.startswith("data:"):
                event["data"] = json.loads(line[5:].strip())
        if "data" in event:
            result.append(event)
    return result


def run(output: Path, *, signed: bool = False) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    report = {"mode": "loopback_mock", "status": "running", "checks": {}, "paid_calls": 0,
              "identity_mode": "hmac" if signed else "demo",
              "catalog": "data/catalog-demo.jsonl.gz", "embedding_dimension": 1024,
              "catalog_sha256": hashlib.sha256((ROOT / "data/catalog-demo.jsonl.gz").read_bytes()).hexdigest()}
    try:
        with tempfile.TemporaryDirectory(prefix="voyacart-http-") as directory, ExitStack() as stack:
            data = Path(directory)
            mock_port, api_port = free_port(), free_port()
            while api_port == mock_port:
                api_port = free_port()
            env = isolated_env(data, mock_url=f"http://127.0.0.1:{mock_port}/v1")
            policy = None
            if signed:
                from app.infrastructure.identity import IdentityPolicy
                # 固定公开测试夹具，只进入临时服务环境；不创建真实持久授权。
                synthetic_secret = "ci-synthetic-HMAC-only-never-production-2026"
                env.update(IDENTITY_MODE="hmac", IDENTITY_HMAC_SECRET=synthetic_secret)
                policy = IdentityPolicy(mode="hmac", secret=synthetic_secret)
            start(stack, "mock_llm:app", mock_port, env, output / "mock.log")
            started = time.monotonic()
            api = start(stack, "app.presentation.server:app", api_port, env, output / "api-cold.log")
            report["cold_start_seconds"] = round(time.monotonic() - started, 3)
            client = stack.enter_context(httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=120, trust_env=False))
            if policy is not None:
                assert client.get("/commerce/identity").status_code == 401
                client.headers["Authorization"] = "Bearer " + policy.issue("ci-buyer")
                assert client.get("/commerce/identity").json()["buyer_id"] == "ci-buyer"
                assert client.get("/commerce/identity/config").json()["mode"] == "hmac"
                assert client.get("/commerce/orders", params={"buyer_id": "other-buyer"}).status_code == 403
                report["checks"]["signed_runtime_identity"] = True
            identity = {"buyer_id": "ci-buyer", "session_id": "ci-session"}
            body = {"threadId": identity["session_id"], "runId": "ci-run", "state": {},
                    "messages": [{"id": "ci-message", "role": "user", "content": "查找寄到中国的旅行背包"}],
                    "tools": [], "context": [], "forwardedProps": {"buyerId": identity["buyer_id"]}}
            response = client.post("/commerce/ag-ui/run", json=body)
            response.raise_for_status()
            events = frames(response.text)
            (output / "events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2))
            assert events[0]["data"]["type"] == "RUN_STARTED"
            assert events[-1]["data"]["type"] == "RUN_FINISHED", events[-1]
            assert all(event["data"]["type"] != "RUN_ERROR" for event in events)
            sequence = [int(event["id"].rsplit(":", 1)[1]) for event in events]
            assert sequence == list(range(1, len(events) + 1)), sequence
            assert any(event["data"]["type"] == "TOOL_CALL_END" for event in events)
            products = [event["data"].get("snapshot", {}).get("products", []) for event in events]
            assert any(products), "检索必须有商品投影"
            report["checks"]["http_sse_search"] = True
            report["event_count"] = len(events)
            midpoint = len(events) // 2
            replay = client.get("/commerce/ag-ui/runs/ci-run/events", params={"buyer_id": "ci-buyer"},
                                headers={"Last-Event-ID": events[midpoint - 1]["id"]})
            replay.raise_for_status()
            assert frames(replay.text) == events[midpoint:]
            report["checks"]["sse_cursor_replay"] = True

            # 持久候选独立于结果区；只接收服务端 run/商品/SKU 引用。
            candidate_url = "/commerce/ag-ui/sessions/ci-session/candidates"
            candidate_query = {"buyer_id": "ci-buyer"}
            candidate_state = client.get(candidate_url, params=candidate_query)
            candidate_state.raise_for_status()
            assert candidate_state.json()["revision"] == 0
            source_cards = next(cards for cards in reversed(products) if cards)
            selections = [(product, sku) for product in source_cards for sku in product["skus"]][:2]
            assert len(selections) == 2
            requests_at_save = httpx.get(f"http://127.0.0.1:{mock_port}/admin/requests", trust_env=False).json()
            for index, (product, sku) in enumerate(selections):
                saved = client.post(candidate_url, params=candidate_query, json={
                    "request_id": f"ci-candidate-{index}", "expected_revision": index,
                    "source": {"kind": "run", "run_id": "ci-run", "product_id": product["product_id"], "sku_id": sku["sku_id"]}})
                saved.raise_for_status()
                candidate_state = saved.json()
                assert candidate_state["items"][-1]["identity_status"] == "bound"
            candidate_ids = [item["candidate_id"] for item in candidate_state["items"]]
            comparison = {"request_id": "ci-comparison", "expected_revision": 2, "candidate_ids": candidate_ids}
            compared = client.put(candidate_url + "/comparison", params=candidate_query, json=comparison)
            compared.raise_for_status()
            candidate_state = compared.json()
            assert candidate_state["comparison_ids"] == candidate_ids
            assert client.put(candidate_url + "/comparison", params=candidate_query, json=comparison).json() == candidate_state
            stale = client.put(candidate_url + "/comparison", params=candidate_query,
                json={"request_id": "ci-stale-tab", "expected_revision": 2, "candidate_ids": []})
            assert stale.status_code == 409 and stale.json()["detail"]["code"] == "CANDIDATE_REVISION_CONFLICT"
            checked = client.get(candidate_url + f"/{candidate_ids[0]}/verification", params={**candidate_query, "ship_to": "CN"})
            checked.raise_for_status()
            assert checked.json()["status"] == "available" and checked.json()["source"] == "local_catalog"
            assert checked.json()["product"]["default_sku_id"] == selections[0][1]["sku_id"]
            assert httpx.get(f"http://127.0.0.1:{mock_port}/admin/requests", trust_env=False).json() == requests_at_save
            assert client.get(candidate_url, params=candidate_query).json() == candidate_state
            (output / "candidates.json").write_text(json.dumps(candidate_state, ensure_ascii=False, indent=2) + "\n")
            (output / "candidate-verification.json").write_text(json.dumps(checked.json(), ensure_ascii=False, indent=2) + "\n")
            report["checks"].update(candidate_save_and_compare=True, candidate_version_conflict=True,
                                    candidate_retry_idempotent=True, candidate_verify_zero_model=True)

            def inventory() -> int:
                with sqlite3.connect(f"file:{data / 'voyacart.db'}?mode=ro", uri=True) as connection:
                    return connection.execute("SELECT stock FROM trade_sku_inventory WHERE sku_id = ?", ("P1001-S1",)).fetchone()[0]

            original = inventory()
            prepare_payload = {**identity, "request_id": "ci-prepare-request",
                "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}],
                "shipping_address": {"recipient_name": "CI 合成买家", "country": "CN", "city": "测试市", "address_line": "合成地址"}}
            prepare = client.post("/commerce/confirmations/orders", json=prepare_payload)
            prepare.raise_for_status()
            card = prepare.json()["confirmation"]
            assert card["status"] == "pending" and inventory() == original
            replay_prepare = client.post("/commerce/confirmations/orders", json=prepare_payload)
            assert replay_prepare.status_code == 200 and replay_prepare.json()["confirmation"] == card
            conflict = client.post("/commerce/confirmations/orders", json={**prepare_payload,
                "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 2}]})
            assert conflict.status_code == 409
            report["checks"]["prepare_request_replay_and_conflict"] = True
            orders = client.get("/commerce/orders", params={"buyer_id": "ci-buyer"}).json()
            assert orders["total"] == 0
            payload = {**identity, "snapshot_hash": card["snapshot_hash"], "approved": True}
            url = f"/commerce/confirmations/{card['confirmation_id']}/resolve"
            approved = client.post(url, json=payload)
            approved.raise_for_status()
            order = approved.json()["order"]
            assert order["status"] == "CONFIRMED" and inventory() == original - 1
            assert order["amount_scope"] == "merchandise_only" and order["order_kind"] == "purchase_intent"
            repeated = client.post(url, json=payload)
            repeated.raise_for_status()
            assert repeated.json()["order"]["order_id"] == order["order_id"] and inventory() == original - 1
            report["checks"]["explicit_confirmation_and_replay"] = True
            report["inventory"] = {"before": original, "after": inventory()}
            requests_before = httpx.get(f"http://127.0.0.1:{mock_port}/admin/requests", trust_env=False).json()
            stop(api)
            started = time.monotonic()
            api = start(stack, "app.presentation.server:app", api_port, env, output / "api-restart.log")
            report["warm_start_seconds"] = round(time.monotonic() - started, 3)
            restored = client.get(f"/commerce/orders/{order['order_id']}", params={"buyer_id": "ci-buyer"})
            restored.raise_for_status()
            assert restored.json()["status"] == "CONFIRMED" and inventory() == original - 1
            repeated = client.post(url, json=payload)
            repeated.raise_for_status()
            assert repeated.json()["order"]["order_id"] == order["order_id"] and inventory() == original - 1
            restored_events = client.get("/commerce/ag-ui/runs/ci-run/events", params={"buyer_id": "ci-buyer"})
            restored_events.raise_for_status()
            restored_frames = frames(restored_events.text)
            (output / "events-restored.json").write_text(json.dumps(restored_frames, ensure_ascii=False, indent=2))
            assert restored_frames == events
            assert httpx.get(f"http://127.0.0.1:{mock_port}/admin/requests", trust_env=False).json() == requests_before
            assert client.get("/commerce/orders", params={"buyer_id": "ci-buyer"}).json()["total"] == 1
            restarted_prepare = client.post("/commerce/confirmations/orders", json=prepare_payload)
            assert restarted_prepare.status_code == 200
            assert restarted_prepare.json()["order"]["order_id"] == order["order_id"]
            report["checks"]["prepare_replay_after_restart"] = True
            assert client.get(candidate_url, params=candidate_query).json() == candidate_state
            assert client.put(candidate_url + "/comparison", params=candidate_query, json=comparison).json() == candidate_state
            checked_after = client.get(candidate_url + f"/{candidate_ids[0]}/verification", params={**candidate_query, "ship_to": "CN"})
            assert checked_after.status_code == 200 and checked_after.json()["status"] == "available"
            assert httpx.get(f"http://127.0.0.1:{mock_port}/admin/requests", trust_env=False).json() == requests_before
            report["checks"]["restart_restores_candidates_comparison_source_and_retry"] = True
            if policy is not None:
                other = {"Authorization": "Bearer " + policy.issue("other-buyer")}
                for path in ("/commerce/ag-ui/runs/ci-run", "/commerce/ag-ui/runs/ci-run/events",
                             f"/commerce/orders/{order['order_id']}", candidate_url,
                             candidate_url + f"/{candidate_ids[0]}/verification"):
                    response = client.get(path, params={"buyer_id": "other-buyer"}, headers=other)
                    assert response.status_code in (403, 404), (path, response.status_code)
                report["checks"]["second_buyer_cannot_replay_or_read_order"] = True
            cancellation_payload = {**identity, "reason": "合成取消验证", "request_id": "ci-cancel-request"}
            cancel_url = f"/commerce/orders/{order['order_id']}/cancel"
            cancellation = client.post(cancel_url, json=cancellation_payload)
            cancellation.raise_for_status()
            cancel_card = cancellation.json()["confirmation"]
            cancelled = client.post(f"/commerce/confirmations/{cancel_card['confirmation_id']}/resolve",
                json={**identity, "snapshot_hash": cancel_card["snapshot_hash"], "approved": True})
            cancelled.raise_for_status()
            assert cancelled.json()["order"]["status"] == "CANCELLED" and inventory() == original
            repeated_cancel = client.post(cancel_url, json=cancellation_payload)
            assert repeated_cancel.status_code == 200 and repeated_cancel.json() == cancelled.json()
            assert inventory() == original
            report["checks"]["cancel_prepare_replay_after_resolution"] = True
            report["inventory"]["after_cancel"] = inventory()
            report["checks"]["restart_restores_order_inventory_and_events"] = True
            report["checks"]["replay_does_not_rerun_model"] = True
            report["mock_chat_requests"] = requests_before["requests"]
            stop(api)
            from qdrant_client import QdrantClient
            index = QdrantClient(path=str(data / "qdrant"))
            try:
                collection = index.get_collection("voyacart_products")
                report["index"] = {"collection": "voyacart_products", "points": collection.points_count,
                    "vector_size": collection.config.params.vectors.size,
                    "distance": str(collection.config.params.vectors.distance)}
                assert collection.points_count == 5700
            finally:
                index.close()
            report["index"]["files_sha256"] = {
                str(path.relative_to(data / "qdrant")): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted((data / "qdrant").rglob("*")) if path.is_file() and path.name != ".lock"}
            report["status"] = "passed"
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--signed", action="store_true", help="用公开合成签名身份验证完整 HTTP 链路")
    args = parser.parse_args()
    print(json.dumps(run(args.output.resolve(), signed=args.signed), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
