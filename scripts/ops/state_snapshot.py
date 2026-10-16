"""停写后的应用恢复组；不探测写入者，不读取 .env，不连接服务。

SQLite backup 只保证单库一致。所有 API、worker、维护任务先统一停写；
传入 Redis 时还必须停止专用 Redis。CLI 仅记录调用者声明，不能证明停写。
manifest 是 0600 的私有恢复材料；CLI 只输出聚合计数，不输出内容或路径。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import time
from typing import Mapping


FORMAT_VERSION = 1
MANIFEST = "manifest.json"
INCOMPLETE = ".snapshot-incomplete"
RESTORE_INCOMPLETE = ".restore-incomplete"
MODEL_KEY = ".secrets/model_registry.key"
SQLITE_PATHS = (
    "voyacart.db", "ag_ui_runs.db", "queue_archive.db", "context_evidence.db",
    "buyer_memory.db", "capabilities.db", "buyer_skills.db", "shopping_forms.db",
    "buyer_favorites.db", "models.sqlite3", "prompts/registry.sqlite3",
    "sessions/session-state.sqlite3", "trade.db",
)
STATE_PATHS = ("queue_maintenance_cursors.json", "orders.json")
DERIVED_PATHS = ("qdrant", "qdrant_kb", "local_embedding")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


class SnapshotError(ValueError):
    """可公开的无内容、无路径错误；底层异常细节不进入 CLI 日志。"""


def _path(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if ".." in raw.parts:
        raise SnapshotError("路径不得包含父目录跳转")
    result = raw.absolute()
    for part in (result, *result.parents):
        if part.is_symlink():
            raise SnapshotError("不支持符号链接路径")
    return result


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise SnapshotError("清单路径无效")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise SnapshotError("清单路径越界")
    return value


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _require_disjoint(paths: list[Path]) -> None:
    for i, left in enumerate(paths):
        if any(_overlap(left, right) for right in paths[i + 1:]):
            raise SnapshotError("源与目标或多个目标不得重叠")


def _fresh(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise SnapshotError("目标必须不存在或为空目录")


def _mkdir(path: Path) -> None:
    _path(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    os.chmod(path, 0o700)


def _write(path: Path, data: bytes) -> None:
    _mkdir(path.parent)
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _copy(source: Path, target: Path) -> None:
    _path(source)
    if not source.is_file() or source.stat().st_nlink != 1:
        raise SnapshotError("仅支持普通单链接文件")
    _mkdir(target.parent)
    with source.open("rb") as src, target.open("xb") as dst:
        os.chmod(target, 0o600)
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    """不跟随链接，也不接受 FIFO/socket/device。"""
    _path(root)
    if not root.is_dir():
        raise SnapshotError("源必须是目录")
    result = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            item = Path(directory) / name
            mode = item.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise SnapshotError("目录含不受支持的链接或特殊文件")
            if item.is_file():
                if item.stat().st_nlink != 1:
                    raise SnapshotError("不支持硬链接文件")
                result.append(item)
    return sorted(result)


def fingerprint_files(paths: Mapping[str, str | Path] | None) -> dict[str, str]:
    """显式 public 源码/配置/锁文件；目录忽略 __pycache__、pyc/pyo。"""
    result = {}
    for label, value in sorted((paths or {}).items()):
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            raise SnapshotError("指纹别名无效")
        path = _path(value)
        files = _files(path) if path.is_dir() else [path]
        if not path.exists():
            raise SnapshotError("指纹输入不存在")
        digest = hashlib.sha256()
        for file in files:
            if path.is_dir() and ("__pycache__" in file.relative_to(path).parts or file.suffix in {".pyc", ".pyo"}):
                continue
            if any(p == ".secrets" or p == ".env" or p.startswith(".env.") for p in file.parts):
                raise SnapshotError("指纹不得读取凭据文件")
            if not file.is_file() or file.stat().st_nlink != 1:
                raise SnapshotError("指纹输入必须是普通文件")
            if path.is_dir():
                digest.update(file.relative_to(path).as_posix().encode())
                digest.update(b"\0")
            digest.update(bytes.fromhex(_hash(file)))
        result[label] = digest.hexdigest() if path.is_dir() else _hash(path)
    return result


def _sqlite_report(path: Path) -> dict:
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise SnapshotError("SQLite 完整性检查失败")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise SnapshotError("SQLite 外键检查失败")
            names = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )]
            counts = {}
            for name in names:
                safe_name = '"' + name.replace('"', '""') + '"'
                counts[name] = db.execute(f"SELECT COUNT(*) FROM {safe_name}").fetchone()[0]
            return {"tables": counts, "table_count": len(counts), "row_count": sum(counts.values())}
    except sqlite3.Error:
        raise SnapshotError("SQLite 读取或完整性检查失败") from None


def _sqlite_backup(source: Path, target: Path) -> dict:
    _mkdir(target.parent)
    _write(target, b"")
    try:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5) as src:
            with sqlite3.connect(target, timeout=5) as dst:
                src.execute("PRAGMA query_only=ON")
                deadline = time.monotonic() + 30

                def progress(_status, _remaining, _total):
                    if time.monotonic() > deadline:
                        raise SnapshotError("SQLite 备份超时；重新核对停写屏障")

                src.backup(dst, pages=256, progress=progress, sleep=0.05)
                # 输出独立 DB，不依赖快照目录中的 WAL/SHM。
                dst.execute("PRAGMA journal_mode=DELETE")
        return _sqlite_report(target)
    except sqlite3.Error:
        raise SnapshotError("SQLite 备份失败") from None


def _state_kind(relative: str) -> str | None:
    if relative in SQLITE_PATHS:
        return "sqlite"
    if relative in STATE_PATHS:
        return "file"
    if relative == MODEL_KEY:
        return "model_key"
    if any(relative == name + suffix for name in SQLITE_PATHS for suffix in ("-wal", "-shm", "-journal")):
        return "sidecar"
    parts = PurePosixPath(relative).parts
    if parts[0] in DERIVED_PATHS:
        return "derived"
    if parts[0] in {"preferences", "sessions"} and relative.endswith(".json"):
        return "file"
    if parts[0] == "conversations" and relative.endswith(".jsonl"):
        return "file"
    return None


def _check_database_url(data_dir: Path, database_url: str | None) -> None:
    if database_url in {None, "file"}:
        return
    prefix = next((p for p in ("sqlite+aiosqlite:///", "sqlite:///") if database_url.startswith(p)), None)
    if not prefix or "?" in database_url or "#" in database_url:
        raise SnapshotError("仅支持 DATA_DIR 内明确的 SQLite 或 file 配置")
    database_path = _path(database_url[len(prefix):])
    if database_path not in {data_dir / "voyacart.db", data_dir / "trade.db"}:
        raise SnapshotError("DATABASE_URL 不在受支持的 DATA_DIR 布局内")
    if not database_path.is_file():
        raise SnapshotError("DATABASE_URL 指定的数据库缺失")


def _validate_key(database: Path, key_file: Path) -> None:
    """只验证可解密，绝不返回明文、密钥或其摘要。"""
    from cryptography.fernet import Fernet, InvalidToken

    _path(key_file)
    if not key_file.is_file() or key_file.stat().st_nlink != 1:
        raise SnapshotError("缺少原模型主密钥")
    try:
        cipher = Fernet(key_file.read_bytes())
    except (ValueError, TypeError):
        raise SnapshotError("模型主密钥格式无效") from None
    checked = 0
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("model_secrets", "model_validation_tickets"):
                if table in tables:
                    for (ciphertext,) in db.execute(f"SELECT ciphertext FROM {table}"):
                        if ciphertext:
                            cipher.decrypt(ciphertext)
                            checked += 1
    except (sqlite3.Error, InvalidToken, TypeError):
        raise SnapshotError("模型凭据无法用原主密钥解密") from None
    if not checked:
        raise SnapshotError("模型数据库缺少可验证的加密记录")


def _entry(area: str, relative: str, path: Path, kind: str, report: dict | None = None) -> dict:
    item = {"area": area, "path": relative, "kind": kind}
    if kind != "model_key":
        item.update({"bytes": path.stat().st_size, "sha256": _hash(path)})
    if report is not None:
        item["sqlite"] = report
    return item


def backup(
    data_dir: str | Path, output: str | Path, *, quiesced: bool = False,
    redis_dir: str | Path | None = None, redis_stopped: bool = False,
    catalog_paths: Mapping[str, str | Path] | None = None,
    fingerprint_paths: Mapping[str, str | Path] | None = None,
    database_url: str | None = None, include_model_key: bool = False,
    model_key_reference: str | None = None,
) -> dict:
    """调用者先建立统一停写屏障；返回私有 manifest，不能直接写公共证据。"""
    if quiesced is not True:
        raise SnapshotError("必须明确声明所有应用写入者已停止")
    source, dest = _path(data_dir), _path(output)
    _fresh(dest)
    redis = _path(redis_dir) if redis_dir is not None else None
    if redis is not None and redis_stopped is not True:
        raise SnapshotError("必须明确声明专用 Redis 已停止")
    inputs = [source] + ([redis] if redis is not None else [])
    _require_disjoint([*inputs, dest])
    _check_database_url(source, database_url)
    files = _files(source)
    if any((source / name).exists() and not (source / name).is_file()
           for name in (*SQLITE_PATHS, *STATE_PATHS, MODEL_KEY)):
        raise SnapshotError("固定状态路径不是普通文件")
    inventory = [(file.relative_to(source).as_posix(), file) for file in files]
    if any(_state_kind(relative) is None for relative, _ in inventory):
        raise SnapshotError("发现未声明的状态文件；拒绝不完整备份")
    for relative, _ in inventory:
        if _state_kind(relative) == "sidecar" and not (source / relative.rsplit("-", 1)[0]).is_file():
            raise SnapshotError("发现缺少主库的 SQLite sidecar")
    has_models = (source / "models.sqlite3").is_file()
    if include_model_key and model_key_reference is not None:
        raise SnapshotError("本地密钥与外部密钥引用不可同时指定")
    if model_key_reference is not None and not _LABEL.fullmatch(model_key_reference):
        raise SnapshotError("外部密钥引用必须是无凭据的安全版本标签")
    if has_models and not (include_model_key or model_key_reference):
        raise SnapshotError("模型数据库需要显式保留原主密钥或外部密钥版本引用")
    if not has_models and (include_model_key or model_key_reference or (source / MODEL_KEY).exists()):
        raise SnapshotError("没有模型数据库，不能单独导出或引用主密钥")
    if include_model_key:
        _validate_key(source / "models.sqlite3", source / MODEL_KEY)
    catalogs: list[tuple[str, Path]] = []
    for label, value in sorted((catalog_paths or {}).items()):
        if not _LABEL.fullmatch(label):
            raise SnapshotError("目录输入别名无效")
        root = _path(value)
        if any(_overlap(root, target) for target in [dest, *inputs]):
            raise SnapshotError("目录输入不得与状态或输出重叠")
        if root.is_dir():
            for file in _files(root):
                catalogs.append((f"{label}/{file.relative_to(root).as_posix()}", file))
        elif root.is_file():
            catalogs.append((label, root))
        else:
            raise SnapshotError("目录输入不存在")
    # 此处也检查配置路径，防止把 .env 或 .secrets 当作 public catalog 打包。
    for _, file in catalogs:
        if any(p == ".secrets" or p == ".env" or p.startswith(".env.") for p in file.parts):
            raise SnapshotError("目录输入不得包含凭据文件")
    fingerprints = fingerprint_files(fingerprint_paths)
    for path in (fingerprint_paths or {}).values():
        if _overlap(_path(path), dest):
            raise SnapshotError("指纹输入不得与输出重叠")
    redis_files = _files(redis) if redis is not None else []
    if redis is not None and not redis_files:
        raise SnapshotError("Redis 持久目录为空，无法证明队列持久状态")
    _mkdir(dest)
    _write(dest / INCOMPLETE, b"incomplete\n")
    entries = []
    directories = []
    if redis is not None:
        for directory in sorted(p for p in redis.rglob("*") if p.is_dir()):
            relative = directory.relative_to(redis).as_posix()
            _mkdir(dest / "redis" / relative)
            directories.append({"area": "redis", "path": relative})
    for label, value in sorted((catalog_paths or {}).items()):
        root = _path(value)
        if root.is_dir():
            for directory in [root, *sorted(p for p in root.rglob("*") if p.is_dir())]:
                suffix = directory.relative_to(root).as_posix()
                relative = label if suffix == "." else f"{label}/{suffix}"
                _mkdir(dest / "catalog" / relative)
                directories.append({"area": "catalog", "path": relative})
    for relative, file in inventory:
        kind = _state_kind(relative)
        if kind in {"sidecar", "derived"} or (kind == "model_key" and not include_model_key):
            continue
        target = dest / "data" / relative
        report = _sqlite_backup(file, target) if kind == "sqlite" else None
        if kind != "sqlite":
            _copy(file, target)
        entries.append(_entry("data", relative, target, kind, report))
    for file in redis_files:
        relative = file.relative_to(redis).as_posix()
        target = dest / "redis" / relative
        _copy(file, target)
        entries.append(_entry("redis", relative, target, "file"))
    for relative, file in catalogs:
        target = dest / "catalog" / relative
        _copy(file, target)
        entries.append(_entry("catalog", relative, target, "file"))
    manifest = {
        "format_version": FORMAT_VERSION, "status": "complete",
        "consistency": "caller_declared_all_writers_stopped",
        "redis_stopped": redis is not None,
        "files": entries, "directories": directories, "fingerprints": fingerprints,
        "absent": [name for name in (*SQLITE_PATHS, *STATE_PATHS) if not (source / name).exists()],
        "excluded": list(DERIVED_PATHS), "index_action": "rebuild_from_frozen_inputs",
        "model_key": {"mode": "included" if include_model_key else "external" if has_models else "not_required",
                      "reference": model_key_reference},
    }
    if include_model_key:
        _validate_key(dest / "data/models.sqlite3", dest / "data" / MODEL_KEY)
    if fingerprint_files(fingerprint_paths) != fingerprints:
        raise SnapshotError("备份过程中源码或配置指纹变化")
    _write(dest / (MANIFEST + ".tmp"), json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2).encode())
    (dest / INCOMPLETE).unlink()
    os.replace(dest / (MANIFEST + ".tmp"), dest / MANIFEST)
    return manifest


def _load_manifest(root: Path) -> dict:
    if not root.is_dir() or (root / INCOMPLETE).exists():
        raise SnapshotError("快照未完成")
    path = _path(root / MANIFEST)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SnapshotError("清单必须是普通单链接文件")
        raw = json.loads(path.read_text())
    except SnapshotError:
        raise
    except (OSError, ValueError):
        raise SnapshotError("缺少有效完整清单") from None
    expected = {"format_version", "status", "consistency", "redis_stopped", "files", "directories", "fingerprints",
                "absent", "excluded", "index_action", "model_key"}
    if not isinstance(raw, dict) or set(raw) != expected or raw["format_version"] != FORMAT_VERSION:
        raise SnapshotError("不支持的快照清单")
    if (raw["status"] != "complete" or raw["consistency"] != "caller_declared_all_writers_stopped"
            or raw["index_action"] != "rebuild_from_frozen_inputs" or raw["excluded"] != list(DERIVED_PATHS)
            or not isinstance(raw["redis_stopped"], bool) or not isinstance(raw["files"], list)
            or not isinstance(raw["directories"], list)
            or not isinstance(raw["fingerprints"], dict) or not isinstance(raw["absent"], list)):
        raise SnapshotError("快照合同无效")
    for label, digest in raw["fingerprints"].items():
        if not _LABEL.fullmatch(label) or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise SnapshotError("快照指纹无效")
    if any(name not in (*SQLITE_PATHS, *STATE_PATHS) for name in raw["absent"]):
        raise SnapshotError("缺失项清单无效")
    return raw


def verify(
    snapshot: str | Path, *, external_model_key_file: str | Path | None = None,
    model_key_reference: str | None = None,
    expected_fingerprints: Mapping[str, str] | None = None,
) -> dict:
    """校验私有清单/所有文件。external 模式无原 key 时可以检验介质，不能恢复。"""
    root = _path(snapshot)
    manifest = _load_manifest(root)
    if expected_fingerprints is not None and dict(expected_fingerprints) != manifest["fingerprints"]:
        raise SnapshotError("源码或配置指纹不匹配")
    expected = {MANIFEST}
    data_paths = set()
    redis_seen = False
    seen_directories = set()
    for item in manifest["directories"]:
        if not isinstance(item, dict) or set(item) != {"area", "path"} or item["area"] not in {"redis", "catalog"}:
            raise SnapshotError("目录清单无效")
        relative = _relative(item["path"])
        name = f'{item["area"]}/{relative}'
        if name in seen_directories or not _path(root / name).is_dir():
            raise SnapshotError("目录清单重复或缺失")
        seen_directories.add(name)
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise SnapshotError("文件清单项无效")
        area, kind = item.get("area"), item.get("kind")
        relative = _relative(item.get("path"))
        if area not in {"data", "redis", "catalog"} or kind not in {"sqlite", "file", "model_key"}:
            raise SnapshotError("文件清单类型无效")
        if (area == "data" and _state_kind(relative) != kind) or (area != "data" and kind != "file"):
            raise SnapshotError("快照包含未知恢复路径")
        keys = {"area", "path", "kind"}
        if kind != "model_key":
            keys |= {"bytes", "sha256"}
        if kind == "sqlite":
            keys.add("sqlite")
        if set(item) != keys:
            raise SnapshotError("文件清单字段无效")
        name = f"{area}/{relative}"
        if name in expected:
            raise SnapshotError("文件清单有重复项")
        expected.add(name)
        path = _path(root / name)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise SnapshotError("快照文件缺失或不是普通文件")
        if kind != "model_key":
            if (type(item["bytes"]) is not int or item["bytes"] < 0 or
                    not isinstance(item["sha256"], str) or not _SHA256.fullmatch(item["sha256"]) or
                    path.stat().st_size != item["bytes"] or _hash(path) != item["sha256"]):
                raise SnapshotError("快照文件校验和不匹配")
        if kind == "sqlite" and _sqlite_report(path) != item["sqlite"]:
            raise SnapshotError("SQLite 聚合清单不匹配")
        if area == "data":
            data_paths.add(relative)
        redis_seen |= area == "redis"
    actual = {file.relative_to(root).as_posix() for file in _files(root)}
    if expected != actual:
        raise SnapshotError("快照含未列出的文件或缺少文件")
    actual_directories = {p.relative_to(root).as_posix() for area in ("redis", "catalog")
                          if (root / area).exists() for p in (root / area).rglob("*") if p.is_dir()}
    if seen_directories != actual_directories:
        raise SnapshotError("目录清单与实际快照不一致")
    if redis_seen != manifest["redis_stopped"]:
        raise SnapshotError("Redis 停机声明与快照不匹配")
    if set(manifest["absent"]) != {name for name in (*SQLITE_PATHS, *STATE_PATHS) if name not in data_paths}:
        raise SnapshotError("已保存与缺失状态清单不一致")
    key = manifest["model_key"]
    if not isinstance(key, dict) or set(key) != {"mode", "reference"}:
        raise SnapshotError("模型主密钥清单无效")
    mode, reference = key["mode"], key["reference"]
    if mode == "included" and "models.sqlite3" in data_paths and MODEL_KEY in data_paths and reference is None:
        if external_model_key_file is not None or model_key_reference is not None:
            raise SnapshotError("快照已包含主密钥，不接受外部替换")
        _validate_key(root / "data/models.sqlite3", root / "data" / MODEL_KEY)
    elif mode == "external" and "models.sqlite3" in data_paths and MODEL_KEY not in data_paths:
        if not isinstance(reference, str) or not _LABEL.fullmatch(reference):
            raise SnapshotError("外部主密钥引用无效")
        if model_key_reference is not None and model_key_reference != reference:
            raise SnapshotError("外部原主密钥版本引用不匹配")
        if external_model_key_file is not None:
            if model_key_reference != reference:
                raise SnapshotError("验证外部主密钥需要原版本引用")
            _validate_key(root / "data/models.sqlite3", _path(external_model_key_file))
    elif mode == "not_required" and "models.sqlite3" not in data_paths and MODEL_KEY not in data_paths and reference is None:
        if external_model_key_file is not None or model_key_reference is not None:
            raise SnapshotError("快照不需要模型主密钥")
    else:
        raise SnapshotError("模型主密钥连续性清单不一致")
    return manifest


def restore(
    snapshot: str | Path, target: str | Path, *, redis_target: str | Path | None = None,
    catalog_target: str | Path | None = None,
    external_model_key_file: str | Path | None = None, model_key_reference: str | None = None,
    expected_fingerprints: Mapping[str, str] | None = None,
) -> dict:
    """校验后仅复制到新目录；不启动服务、不迁移、不重建索引、不生成新主密钥。"""
    root, dest = _path(snapshot), _path(target)
    targets = {"data": dest}
    if redis_target is not None:
        targets["redis"] = _path(redis_target)
    if catalog_target is not None:
        targets["catalog"] = _path(catalog_target)
    _require_disjoint([root, *targets.values()])
    for directory in targets.values():
        _fresh(directory)
    manifest = verify(root, external_model_key_file=external_model_key_file,
                      model_key_reference=model_key_reference, expected_fingerprints=expected_fingerprints)
    if manifest["fingerprints"] and expected_fingerprints is None:
        raise SnapshotError("恢复必须显式核对冻结的源码与配置指纹")
    if any(item["area"] not in targets for item in [*manifest["files"], *manifest["directories"]]):
        raise SnapshotError("必须为每个恢复组件提供独立新目录")
    if manifest["model_key"]["mode"] == "external" and external_model_key_file is None:
        raise SnapshotError("恢复缺少外部原模型主密钥及其版本引用")
    if external_model_key_file is not None and any(_overlap(_path(external_model_key_file), p) for p in targets.values()):
        raise SnapshotError("外部主密钥输入与恢复目标不得重叠")
    for directory in targets.values():
        _mkdir(directory)
        _write(directory / RESTORE_INCOMPLETE, b"incomplete\n")
    for item in manifest["directories"]:
        _mkdir(targets[item["area"]] / item["path"])
    for item in manifest["files"]:
        source = root / item["area"] / item["path"]
        destination = targets[item["area"]] / item["path"]
        _copy(source, destination)
        if item["kind"] != "model_key" and _hash(destination) != item["sha256"]:
            raise SnapshotError("恢复后文件校验和不匹配")
        if item["kind"] == "sqlite" and _sqlite_report(destination) != item["sqlite"]:
            raise SnapshotError("恢复后 SQLite 校验失败")
    if manifest["model_key"]["mode"] == "included":
        _validate_key(dest / "models.sqlite3", dest / MODEL_KEY)
    # external 模式只校验调用者提供的原密钥，不复制；启动时仍须由原外部密钥机制提供。
    for directory in targets.values():
        (directory / RESTORE_INCOMPLETE).unlink()
    return manifest


def summary(manifest: dict) -> dict:
    """可用于公开合成证据；不输出原路径、表名、密钥引用或密文。"""
    return {"status": "verified", "format_version": manifest["format_version"],
            "files": len(manifest["files"]),
            "sqlite_databases": sum(item["kind"] == "sqlite" for item in manifest["files"]),
            "sqlite_rows": sum(item.get("sqlite", {}).get("row_count", 0) for item in manifest["files"]),
            "redis_files": sum(item["area"] == "redis" for item in manifest["files"]),
            "catalog_files": sum(item["area"] == "catalog" for item in manifest["files"]),
            "fingerprint_count": len(manifest["fingerprints"]),
            "configuration_bound": bool(manifest["fingerprints"]),
            "catalog_rebuild_inputs_included": any(item["area"] == "catalog" for item in manifest["files"]),
            "model_key_mode": manifest["model_key"]["mode"],
            "model_key_verified": manifest["model_key"]["mode"] == "included",
            "consistency": manifest["consistency"], "derived_indexes": "rebuild_required"}


def _pairs(values: list[str]) -> dict[str, Path]:
    result = {}
    for value in values:
        label, separator, path = value.partition("=")
        if not separator or not path or not _LABEL.fullmatch(label) or label in result:
            raise SnapshotError("输入必须是唯一的 安全别名=路径")
        result[label] = Path(path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("backup", help="调用者先停止所有写入者；此命令不证明停写")
    create.add_argument("--data-dir", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--quiesced", action="store_true")
    create.add_argument("--redis-dir")
    create.add_argument("--redis-stopped", action="store_true")
    create.add_argument("--catalog", action="append", default=[], metavar="LABEL=PATH")
    create.add_argument("--fingerprint", action="append", default=[], metavar="LABEL=PATH")
    create.add_argument("--database-url")
    create.add_argument("--include-model-key", action="store_true", help="显式保留原主密钥；本轮仅用合成临时 fixture")
    create.add_argument("--model-key-reference")
    for name in ("verify", "restore"):
        command = sub.add_parser(name)
        command.add_argument("--snapshot", required=True)
        command.add_argument("--external-model-key-file")
        command.add_argument("--model-key-reference")
        command.add_argument("--fingerprint", action="append", default=[], metavar="LABEL=PATH")
        if name == "restore":
            command.add_argument("--target", required=True)
            command.add_argument("--redis-target")
            command.add_argument("--catalog-target")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            manifest = backup(args.data_dir, args.output, quiesced=args.quiesced,
                              redis_dir=args.redis_dir, redis_stopped=args.redis_stopped,
                              catalog_paths=_pairs(args.catalog), fingerprint_paths=_pairs(args.fingerprint),
                              database_url=args.database_url, include_model_key=args.include_model_key,
                              model_key_reference=args.model_key_reference)
        else:
            options = {"external_model_key_file": args.external_model_key_file,
                       "model_key_reference": args.model_key_reference,
                       "expected_fingerprints": fingerprint_files(_pairs(args.fingerprint)) if args.fingerprint else None}
            manifest = (verify(args.snapshot, **options) if args.command == "verify" else
                        restore(args.snapshot, args.target, redis_target=args.redis_target,
                                catalog_target=args.catalog_target, **options))
        result = summary(manifest)
        result["operation"] = args.command
        if getattr(args, "external_model_key_file", None):
            result["model_key_verified"] = True
        print(json.dumps(result, sort_keys=True))
        return 0
    except SnapshotError as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        print(json.dumps({"status": "failed", "error": "快照文件操作或清单验证失败；未完成输出不可用于恢复"}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
