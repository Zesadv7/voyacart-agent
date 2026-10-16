"""T1 的数据边界、确定性抽样和缓存验收。"""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts.esci.download import FILES, download, verify_files
from scripts.esci.prepare import CATEGORIES, PROMPT_VERSION, candidate_pool, classify, json_line, read_cache


@pytest.mark.parametrize('separator', ['\u2028', '\u2029', '\u0085'])
def test_query_cache_preserves_unicode_line_separators(tmp_path, separator):
    row = {'locale': 'us', 'query_id': 1, 'prompt_version': PROMPT_VERSION,
           'query': f'原文{separator}查询', 'split': 'train', 'status': 'ok', 'category': CATEGORIES[0]}
    second = {**row, 'query_id': 2, 'query': '第二条查询'}
    path = tmp_path / 'cache.jsonl'
    path.write_text(json_line(row) + json.dumps(second, ensure_ascii=False))
    assert read_cache(path) == {('us', 1, PROMPT_VERSION): row, ('us', 2, PROMPT_VERSION): second}


def test_download_checks_upstream_and_saved_fingerprints(tmp_path):
    content = b"dataset bytes"

    def respond(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"sha": "frozen-revision"})
        if request.url.host == "raw.githubusercontent.com":
            pointer = f"oid blake2b:{hashlib.blake2b(content).hexdigest()}\nsize {len(content)}\n"
            return httpx.Response(200, text=pointer)
        return httpx.Response(200, content=content)

    raw, manifest = tmp_path / "raw", tmp_path / "source.json"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        source = download(raw, manifest, client)
        assert source["source_commit"] == "frozen-revision"
        verify_files(raw, source)
        assert download(raw, manifest, client) == source
        (raw / FILES[0]).write_bytes(b"changed bytes")
        with pytest.raises(ValueError, match="内容指纹不符"):
            download(raw, manifest, client)
    assert (raw / FILES[0]).read_bytes() == b"changed bytes"


def test_download_rejects_incomplete_file(tmp_path):
    def respond(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"sha": "frozen-revision"})
        if request.url.host == "raw.githubusercontent.com":
            return httpx.Response(200, text=f"oid blake2b:{hashlib.blake2b(b'full').hexdigest()}\nsize 4\n")
        return httpx.Response(200, content=b"cut")

    raw, manifest = tmp_path / "raw", tmp_path / "source.json"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="上游内容指纹不符"):
            download(raw, manifest, client)
    assert not (raw / FILES[0]).exists()
    assert not manifest.exists()


def test_pool_uses_small_version_unique_queries_and_fixed_ratios(tmp_path):
    rows = []
    for locale in ("us", "es", "jp"):
        for split, base in (("train", 0), ("test", 100)):
            for index in range(20):
                row = {"query_id": base + index, "query": f"query {index}", "product_locale": locale,
                       "split": split, "small_version": int(index < 10)}
                rows.extend([row, row])
    path = tmp_path / "examples.parquet"
    pq.write_table(pa.Table.from_pylist(rows), path)
    first = candidate_pool(path, per_split=12)
    pq.write_table(pa.Table.from_pylist(list(reversed(rows))), path)
    assert candidate_pool(path, per_split=12) == first
    assert len(first) == 24
    assert len({(row["locale"], row["query_id"]) for row in first}) == 24
    for split in ("train", "test"):
        assert [sum(row["locale"] == locale and row["split"] == split for row in first)
                for locale in ("us", "es", "jp")] == [6, 3, 3]
    assert all(row["query_id"] % 100 < 10 for row in first)
    smaller = candidate_pool(path, per_split=4)
    assert len(smaller) == 8
    assert {(row["locale"], row["query_id"]) for row in smaller} <= {(row["locale"], row["query_id"]) for row in first}
    for count in (0, -4, 3):
        with pytest.raises(ValueError, match="正整数且能被 4 整除"):
            candidate_pool(path, per_split=count)
    with pytest.raises(ValueError, match="候选池不足"):
        candidate_pool(path)


async def test_model_cache_replay_invalid_output_and_concurrency(tmp_path):
    calls = active = maximum = 0

    async def respond(request):
        nonlocal calls, active, maximum
        calls += 1
        active += 1
        maximum = max(maximum, active)
        payload = json.loads(request.content)
        assert payload["temperature"] == 0
        assert payload["response_format"] == {"type": "json_object"}
        query = json.loads(payload["messages"][1]["content"])["query"]
        await asyncio.sleep(0.01)
        active -= 1
        category = "越界类目" if query == "invalid" else "旅行装备"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"category": category})}}]})

    settings = SimpleNamespace(llm_base_url="https://model.example/v1", llm_api_key="test-only-credential",
                               llm_model="test-model", llm_max_concurrency=2, llm_min_interval_seconds=0,
                               llm_max_retries=2)
    rows = [{"locale": "us", "split": "train", "query_id": index,
             "query": "invalid" if index == 3 else "backpack"} for index in range(8)]
    cache = tmp_path / "cache.jsonl"
    first, replay = tmp_path / "first", tmp_path / "replay"
    first.mkdir()
    replay.mkdir()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await classify(rows, cache, first, settings, client)
        before = cache.read_bytes()
        replay_report = await classify(rows, cache, replay, settings, client)
    assert maximum == 2
    assert calls == report["model_calls"] == 8
    assert report["failed_outputs"] == report["cached_failure_count"] == 1
    assert replay_report["model_calls"] == 0
    assert replay_report["cache_hits"] == 8
    assert replay_report["distribution"] == report["distribution"]
    assert {(row["locale"], row["split"]): row["required"] for row in report["coverage"]} == {
        ("us", "train"): 130, ("us", "test"): 195,
        ("es", "train"): 65, ("es", "test"): 98,
        ("jp", "train"): 65, ("jp", "test"): 98,
    }
    assert cache.read_bytes() == before
    assert len(read_cache(cache)) == 8
    assert (first / "failures.jsonl").read_bytes().count(b'\n') == 1
    assert settings.llm_api_key not in (first / "requests.jsonl").read_text()


async def test_service_failure_preserves_failure_and_stops(tmp_path):
    settings = SimpleNamespace(llm_base_url="https://model.example/v1", llm_api_key="test-only-credential",
                               llm_model="test-model", llm_max_concurrency=1, llm_min_interval_seconds=0,
                               llm_max_retries=2)
    rows = [{"locale": "us", "split": "test", "query_id": 1, "query": "backpack"}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        with pytest.raises(ExceptionGroup, match="TaskGroup"):
            await classify(rows, tmp_path / "cache.jsonl", tmp_path, settings, client)
    failure = json.loads((tmp_path / "failures.jsonl").read_text())
    assert failure["http_status"] == 503
    assert read_cache(tmp_path / "cache.jsonl") == {}
    assert json.loads((tmp_path / "call-counts.json").read_text())["model_calls"] == 1


@pytest.mark.parametrize("recovers", [True, False])
async def test_timeout_retry_records_attempts_and_stops_at_limit(tmp_path, monkeypatch, recovers):
    calls = 0
    delays = []

    async def wait(delay):
        delays.append(delay)

    def respond(request):
        nonlocal calls
        calls += 1
        if calls < 3 or not recovers:
            raise httpx.ReadTimeout("request timed out", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"category":"旅行装备"}'}}]})

    monkeypatch.setattr("scripts.esci.prepare.asyncio.sleep", wait)
    settings = SimpleNamespace(llm_base_url="https://model.example/v1", llm_api_key="test-only-credential",
                               llm_model="test-model", llm_max_concurrency=1, llm_min_interval_seconds=0,
                               llm_max_retries=2)
    rows = [{"locale": "us", "split": "test", "query_id": 1, "query": "backpack"}]
    cache = tmp_path / "cache.jsonl"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        if recovers:
            report = await classify(rows, cache, tmp_path, settings, client)
            assert report["completed_calls"] == 1
        else:
            with pytest.raises(ExceptionGroup, match="TaskGroup"):
                await classify(rows, cache, tmp_path, settings, client)
    with (tmp_path / "failures.jsonl").open(encoding='utf-8') as stream:
        failures = [json.loads(line) for line in stream]
    assert calls == 3
    assert delays == [6, 18]
    assert [row["attempt"] for row in failures] == ([1, 2] if recovers else [1, 2, 3])
    assert [row["retry"] for row in failures] == ([True, True] if recovers else [True, True, False])
    assert len(read_cache(cache)) == int(recovers)
    assert json.loads((tmp_path / "call-counts.json").read_text())["model_calls"] == 3


def test_categories_match_runtime_contract():
    from app.application.tools.product_search_tool import _KNOWN_CATEGORIES

    assert CATEGORIES == _KNOWN_CATEGORIES
