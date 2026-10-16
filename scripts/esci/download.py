"""显式下载 ESCI 原始文件，并核对上游内容指纹。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
FILES = (
    "shopping_queries_dataset_examples.parquet",
    "shopping_queries_dataset_products.parquet",
)
MEDIA = "https://media.githubusercontent.com/media/amazon-science/esci-data/main/shopping_queries_dataset/"


def fingerprint(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "blake2b").hexdigest()


def verify_files(raw: Path, source: dict) -> None:
    for name in FILES:
        path = raw / name
        expected = source["files"][name]
        if path.stat().st_size != expected["bytes"] or fingerprint(path) != expected["content_fingerprint"]:
            raise ValueError(f"{name} 的大小或内容指纹不符")


def download(raw: Path, manifest: Path, client: httpx.Client) -> dict:
    if manifest.exists():
        source = json.loads(manifest.read_text())
    else:
        response = client.get("https://api.github.com/repos/amazon-science/esci-data/commits/main")
        response.raise_for_status()
        source = {"source_commit": response.json()["sha"], "fingerprint_method": "blake2b", "files": {}}
    raw.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        path = raw / name
        expected = source["files"].get(name)
        if path.exists() and expected:
            if path.stat().st_size != expected["bytes"] or fingerprint(path) != expected["content_fingerprint"]:
                raise ValueError(f"{name} 的大小或内容指纹不符")
            print(f"已校验 {name}：{expected['bytes']} 字节", flush=True)
            continue
        pointer_url = f"https://raw.githubusercontent.com/amazon-science/esci-data/{source['source_commit']}/shopping_queries_dataset/{name}"
        pointer = client.get(pointer_url)
        pointer.raise_for_status()
        fields = dict(line.split(" ", 1) for line in pointer.text.splitlines())
        algorithm, upstream_fingerprint = fields["oid"].split(":", 1)
        upstream_digest = hashlib.new(algorithm)
        local_digest = hashlib.blake2b()
        temporary = path.with_suffix(".part")
        print(f"开始下载 {name}：{fields['size']} 字节", flush=True)
        with client.stream("GET", MEDIA + name) as response:
            response.raise_for_status()
            with temporary.open("wb") as stream:
                for chunk in response.iter_bytes(1024 * 1024):
                    stream.write(chunk)
                    upstream_digest.update(chunk)
                    local_digest.update(chunk)
        if temporary.stat().st_size != int(fields["size"]) or upstream_digest.hexdigest() != upstream_fingerprint:
            raise ValueError(f"{name} 与上游内容指纹不符")
        entry = {"url": MEDIA + name, "bytes": temporary.stat().st_size,
                 "content_fingerprint": local_digest.hexdigest(), "upstream_content_fingerprint": upstream_fingerprint}
        if expected and entry != expected:
            raise ValueError(f"{name} 与 source.json 内容指纹不符")
        temporary.replace(path)
        source["files"][name] = entry
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(source, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        print(f"已下载并校验 {name}：{entry['bytes']} 字节", flush=True)
    return source
