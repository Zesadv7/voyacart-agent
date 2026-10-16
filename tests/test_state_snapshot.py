"""全部状态/密钥均在 tmp_path 合成；不读取运行 data、环境或真实凭据。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3

from cryptography.fernet import Fernet
import pytest

from scripts.ops import state_snapshot as snapshots


def make_db(path: Path, *, wal: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    if wal:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("INSERT INTO records VALUES (1, 'synthetic-before-snapshot')")
    db.commit()
    return db


def model_state(data: Path) -> Path:
    """随机生成的临时 fixture 密钥只用于本测试，不进入日志或证据。"""
    (data / ".secrets").mkdir(parents=True)
    key_file = data / snapshots.MODEL_KEY
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    with sqlite3.connect(data / "models.sqlite3") as db:
        db.executescript("""
            CREATE TABLE model_secrets (secret_ref TEXT PRIMARY KEY, ciphertext BLOB NOT NULL);
            CREATE TABLE model_validation_tickets (ciphertext BLOB NOT NULL);
        """)
        cipher = Fernet(key)
        db.execute("INSERT INTO model_secrets VALUES (?,?)", ("synthetic-reference", cipher.encrypt(b"synthetic-only-token")))
        db.execute("INSERT INTO model_validation_tickets VALUES (?)", (cipher.encrypt(b"synthetic-only-ticket"),))
    return key_file


def small_snapshot(tmp_path: Path) -> tuple[Path, Path]:
    data, output = tmp_path / "source", tmp_path / "snapshot"
    make_db(data / "voyacart.db").close()
    snapshots.backup(data, output, quiesced=True)
    return data, output


def change_manifest(snapshot: Path, mutate) -> None:
    path = snapshot / snapshots.MANIFEST
    manifest = json.loads(path.read_text())
    mutate(manifest)
    path.write_text(json.dumps(manifest))


def test_multidatabase_wal_backup_restore_keeps_snapshot_boundary(tmp_path):
    source, output, target = [tmp_path / name for name in ("source", "snapshot", "restored")]
    writers = []
    try:
        for relative in snapshots.SQLITE_PATHS:
            if relative == "models.sqlite3":
                continue
            writers.append(make_db(source / relative, wal=True))
        assert (source / "voyacart.db-wal").stat().st_size > 0
        # 保留连接以确保未 checkpoint 的已提交 WAL 存在，但本窗口不执行任何写入。
        unchanged_files = [p for p in source.rglob("*") if p.is_file() and not p.name.endswith("-shm")]
        source_before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in unchanged_files}
        manifest = snapshots.backup(source, output, quiesced=True)
        assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in unchanged_files} == source_before
        assert len([f for f in manifest["files"] if f["kind"] == "sqlite"]) == 12
        assert not list(output.rglob("*-wal"))
        for writer in writers:
            writer.execute("INSERT INTO records VALUES (2, 'synthetic-after-snapshot')")
            writer.commit()
        snapshots.restore(output, target)
        for relative in snapshots.SQLITE_PATHS:
            if relative == "models.sqlite3":
                continue
            with sqlite3.connect(target / relative) as db:
                assert db.execute("SELECT * FROM records").fetchall() == [(1, "synthetic-before-snapshot")]
        assert writers[0].execute("SELECT COUNT(*) FROM records").fetchone() == (2,)
        assert not (target / snapshots.RESTORE_INCOMPLETE).exists()
        assert output.stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in output.rglob("*") if p.is_file())
    finally:
        for writer in writers:
            writer.close()


def test_known_file_state_and_derived_indexes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    state = {"preferences/v2/synthetic.json": b'{"color":"synthetic"}',
             "sessions/synthetic.json": b"{}", "conversations/synthetic.jsonl": b"{}\n",
             "queue_maintenance_cursors.json": b'{"cursor":2}', "orders.json": b"[]"}
    for name, value in state.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    for name in snapshots.DERIVED_PATHS:
        path = source / name / "synthetic-index"
        path.parent.mkdir()
        path.write_text("derived-only")
    output, target = tmp_path / "snapshot", tmp_path / "restored"
    manifest = snapshots.backup(source, output, quiesced=True, database_url="file")
    snapshots.restore(output, target)
    assert {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()} == state
    assert manifest["excluded"] == list(snapshots.DERIVED_PATHS)
    assert set(snapshots.SQLITE_PATHS).issubset(manifest["absent"])


def test_model_key_continuity_and_private_manifest(tmp_path, capsys):
    source, output, target = [tmp_path / name for name in ("source", "snapshot", "restored")]
    key_file = model_state(source)
    manifest = snapshots.backup(source, output, quiesced=True, include_model_key=True)
    text = (output / snapshots.MANIFEST).read_text()
    assert key_file.read_text() not in text
    assert hashlib.sha256(key_file.read_bytes()).hexdigest() not in text
    key_entry = next(f for f in manifest["files"] if f["kind"] == "model_key")
    assert set(key_entry) == {"area", "path", "kind"}
    snapshots.restore(output, target)
    assert (target / snapshots.MODEL_KEY).read_bytes() == key_file.read_bytes()
    assert snapshots.main(["verify", "--snapshot", str(output)]) == 0
    printed = capsys.readouterr().out
    assert key_file.read_text() not in printed and "synthetic-reference" not in printed
    assert "model_registry.key" not in printed and "ciphertext" not in printed


@pytest.mark.parametrize("case", ["unapproved", "missing", "wrong", "malformed"])
def test_missing_wrong_or_unapproved_model_key_fails(tmp_path, case):
    source, output = tmp_path / "source", tmp_path / "snapshot"
    key_file = model_state(source)
    if case == "missing":
        key_file.unlink()
    elif case == "wrong":
        key_file.write_bytes(Fernet.generate_key())
    elif case == "malformed":
        key_file.write_bytes(b"not-a-key")
    with pytest.raises(snapshots.SnapshotError):
        snapshots.backup(source, output, quiesced=True, include_model_key=case != "unapproved")
    assert not (output / snapshots.MANIFEST).exists()


def test_key_tampering_fails_without_key_hash(tmp_path):
    source, output = tmp_path / "source", tmp_path / "snapshot"
    model_state(source)
    snapshots.backup(source, output, quiesced=True, include_model_key=True)
    (output / "data" / snapshots.MODEL_KEY).write_bytes(Fernet.generate_key())
    with pytest.raises(snapshots.SnapshotError, match="解密"):
        snapshots.restore(output, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_external_key_requires_original_key_and_reference_on_restore(tmp_path):
    source, output = tmp_path / "source", tmp_path / "snapshot"
    key_file = model_state(source)
    snapshots.backup(source, output, quiesced=True, model_key_reference="fixture-key-v1")
    assert not (output / "data" / snapshots.MODEL_KEY).exists()
    assert snapshots.summary(snapshots.verify(output))["model_key_verified"] is False
    with pytest.raises(snapshots.SnapshotError, match="缺少外部"):
        snapshots.restore(output, tmp_path / "target")
    with pytest.raises(snapshots.SnapshotError, match="版本引用"):
        snapshots.restore(output, tmp_path / "target", external_model_key_file=key_file, model_key_reference="wrong-v2")
    snapshots.restore(output, tmp_path / "target", external_model_key_file=key_file, model_key_reference="fixture-key-v1")
    assert not (tmp_path / "target" / snapshots.MODEL_KEY).exists()
    snapshots._validate_key(tmp_path / "target/models.sqlite3", key_file)


def test_complete_redis_multipart_directory_and_catalog_binding(tmp_path):
    source, redis, catalog = [tmp_path / name for name in ("source", "redis-source", "catalog-source")]
    make_db(source / "voyacart.db").close()
    redis.mkdir()
    (redis / "empty-persistence-subdir").mkdir()
    redis_files = {"appendonlydir/appendonly.aof.manifest": b"file appendonly.aof.1.base.rdb seq 1 type b\nfile appendonly.aof.1.incr.aof seq 1 type i\n",
                   "appendonlydir/appendonly.aof.1.base.rdb": b"synthetic-base",
                   "appendonlydir/appendonly.aof.1.incr.aof": b"synthetic-incremental",
                   "dump.rdb": b"synthetic-dump"}
    for name, value in redis_files.items():
        file = redis / name
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(value)
    catalog.mkdir()
    (catalog / "catalog.json").write_text('{"synthetic": true}')
    config = tmp_path / "config.json"
    config.write_text('{"embedding_model":"mock","dimension":8}')
    fingerprint_paths = {"config": config, "catalog-input": catalog}
    expected = snapshots.fingerprint_files(fingerprint_paths)
    output = tmp_path / "snapshot"
    manifest = snapshots.backup(source, output, quiesced=True, redis_dir=redis, redis_stopped=True,
                                catalog_paths={"runtime": catalog}, fingerprint_paths=fingerprint_paths)
    with pytest.raises(snapshots.SnapshotError, match="指纹"):
        snapshots.restore(output, tmp_path / "target", redis_target=tmp_path / "redis-target", catalog_target=tmp_path / "catalog-target")
    with pytest.raises(snapshots.SnapshotError, match="组件"):
        snapshots.restore(output, tmp_path / "target", expected_fingerprints=expected)
    snapshots.restore(output, tmp_path / "target", redis_target=tmp_path / "redis-target", catalog_target=tmp_path / "catalog-target", expected_fingerprints=expected)
    assert {p.relative_to(tmp_path / "redis-target").as_posix(): p.read_bytes() for p in (tmp_path / "redis-target").rglob("*") if p.is_file()} == redis_files
    assert (tmp_path / "catalog-target/runtime/catalog.json").read_bytes() == (catalog / "catalog.json").read_bytes()
    assert snapshots.summary(manifest)["redis_files"] == 4
    assert (tmp_path / "redis-target/empty-persistence-subdir").is_dir()
    config.write_text("changed")
    with pytest.raises(snapshots.SnapshotError, match="指纹"):
        snapshots.restore(output, tmp_path / "other-target", expected_fingerprints=snapshots.fingerprint_files(fingerprint_paths))


@pytest.mark.parametrize("case", ["quiescence", "redis_running", "empty_redis", "unknown_file", "external_db", "orphan_wal", "overlap", "nonempty"])
def test_backup_rejects_unsafe_or_incomplete_inputs(tmp_path, case):
    source, output = tmp_path / "source", tmp_path / "snapshot"
    make_db(source / "voyacart.db").close()
    kwargs = {"quiesced": True}
    if case == "quiescence":
        kwargs["quiesced"] = False
    elif case in {"redis_running", "empty_redis"}:
        redis = tmp_path / "redis"
        redis.mkdir()
        kwargs.update(redis_dir=redis, redis_stopped=case == "empty_redis")
    elif case == "unknown_file":
        (source / "unlisted.db").write_bytes(b"unknown")
    elif case == "external_db":
        kwargs["database_url"] = f"sqlite+aiosqlite:///{tmp_path / 'external.db'}"
    elif case == "orphan_wal":
        (source / "ag_ui_runs.db-wal").write_bytes(b"orphan")
    elif case == "overlap":
        output = source / "snapshot"
    elif case == "nonempty":
        output.mkdir()
        (output / "existing").write_text("do not touch")
    with pytest.raises(snapshots.SnapshotError):
        snapshots.backup(source, output, **kwargs)
    assert not (output / snapshots.MANIFEST).exists()


@pytest.mark.parametrize("case", ["missing", "tamper", "extra", "incomplete", "traversal", "absolute", "duplicate", "unknown", "extra_field", "wrong_absent"])
def test_verify_rejects_corrupt_partial_and_malicious_snapshots(tmp_path, case):
    _, output = small_snapshot(tmp_path)
    path = output / "data/voyacart.db"
    if case == "missing":
        path.unlink()
    elif case == "tamper":
        with path.open("ab") as file:
            file.write(b"tampered")
    elif case == "extra":
        (output / "unknown").write_text("extra")
    elif case == "incomplete":
        (output / snapshots.INCOMPLETE).write_text("incomplete")
    elif case in {"traversal", "absolute", "unknown"}:
        name = {"traversal": "../../outside.db", "absolute": "/tmp/outside.db", "unknown": "unlisted.db"}[case]
        change_manifest(output, lambda m: m["files"][0].update(path=name))
    elif case == "duplicate":
        change_manifest(output, lambda m: m["files"].append(m["files"][0]))
    elif case == "extra_field":
        change_manifest(output, lambda m: m.update(unexpected=True))
    elif case == "wrong_absent":
        change_manifest(output, lambda m: m["absent"].append("voyacart.db"))
    with pytest.raises(snapshots.SnapshotError):
        snapshots.restore(output, tmp_path / "target")
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("where", ["source_file", "source_parent", "output", "snapshot_file", "target", "hardlink", "fifo"])
def test_rejects_links_special_files_and_target_escape(tmp_path, where):
    source, output = small_snapshot(tmp_path)
    target = tmp_path / "target"
    if where == "source_file":
        (source / "voyacart.db").unlink()
        (source / "voyacart.db").symlink_to(output / "data/voyacart.db")
    elif where == "source_parent":
        alias = tmp_path / "alias"
        alias.symlink_to(source, target_is_directory=True)
        source = alias
    elif where == "output":
        alias = tmp_path / "alias"
        alias.symlink_to(output, target_is_directory=True)
        output = alias
    elif where == "snapshot_file":
        (output / "data/voyacart.db").unlink()
        (output / "data/voyacart.db").symlink_to(source / "voyacart.db")
    elif where == "target":
        target.symlink_to(source, target_is_directory=True)
    elif where == "hardlink":
        os.link(source / "voyacart.db", source / "trade.db")
    elif where == "fifo":
        os.mkfifo(source / "queue_maintenance_cursors.json")
    with pytest.raises(snapshots.SnapshotError):
        if where in {"source_file", "source_parent", "hardlink", "fifo"}:
            snapshots.backup(source, tmp_path / "new-snapshot", quiesced=True)
        else:
            snapshots.restore(output, target)


def test_restore_rejects_nonempty_and_overlapping_targets(tmp_path):
    source, output = small_snapshot(tmp_path)
    for target in (source, output, output / "child", tmp_path):
        with pytest.raises(snapshots.SnapshotError):
            snapshots.restore(output, target)
    with pytest.raises(snapshots.SnapshotError):
        snapshots.restore(output, tmp_path / "restored", redis_target=tmp_path / "restored/redis")


def test_failure_keeps_incomplete_marker_and_no_complete_manifest(tmp_path, monkeypatch):
    source = tmp_path / "source"
    make_db(source / "voyacart.db").close()
    (source / "queue_maintenance_cursors.json").write_text("{}")
    output = tmp_path / "snapshot"

    def fail(*_args):
        raise OSError("synthetic-copy-failure")

    monkeypatch.setattr(snapshots, "_copy", fail)
    with pytest.raises(OSError):
        snapshots.backup(source, output, quiesced=True)
    assert (output / snapshots.INCOMPLETE).exists()
    assert not (output / snapshots.MANIFEST).exists()
    with pytest.raises(snapshots.SnapshotError):
        snapshots.verify(output)


def test_restore_failure_preserves_incomplete_marker(tmp_path, monkeypatch):
    _, output = small_snapshot(tmp_path)
    target = tmp_path / "target"

    def fail(*_args):
        raise OSError("synthetic-copy-failure")

    monkeypatch.setattr(snapshots, "_copy", fail)
    with pytest.raises(OSError):
        snapshots.restore(output, target)
    assert (target / snapshots.RESTORE_INCOMPLETE).exists()
    with pytest.raises(snapshots.SnapshotError, match="未声明"):
        snapshots.backup(target, tmp_path / "new-snapshot", quiesced=True)


def test_foreign_key_and_invalid_database_fail_closed(tmp_path):
    source, output = tmp_path / "source", tmp_path / "snapshot"
    source.mkdir()
    with sqlite3.connect(source / "voyacart.db") as db:
        db.executescript("CREATE TABLE parent(id INTEGER PRIMARY KEY); CREATE TABLE child(id INTEGER REFERENCES parent(id)); INSERT INTO child VALUES(1);")
    with pytest.raises(snapshots.SnapshotError, match="外键"):
        snapshots.backup(source, output, quiesced=True)
    assert (output / snapshots.INCOMPLETE).exists()
    assert not (output / snapshots.MANIFEST).exists()


def test_cli_round_trip_and_sanitized_failure(tmp_path, capsys):
    source = tmp_path / "source"
    make_db(source / "voyacart.db").close()
    output, target = tmp_path / "snapshot", tmp_path / "target"
    assert snapshots.main(["backup", "--data-dir", str(source), "--output", str(output), "--quiesced"]) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "backup"
    assert snapshots.main(["restore", "--snapshot", str(output), "--target", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "restore"
    assert snapshots.main(["restore", "--snapshot", str(output), "--target", str(target)]) == 2
    result = capsys.readouterr()
    assert not result.out
    assert str(tmp_path) not in result.err
    assert json.loads(result.err)["status"] == "failed"


def test_fingerprint_rejects_secret_inputs_and_tracks_directory_changes(tmp_path):
    public = tmp_path / "public"
    public.mkdir()
    (public / "config.json").write_text("{}"); (public / "code.py").write_text("pass")
    before = snapshots.fingerprint_files({"source": public})
    (public / "__pycache__").mkdir()
    (public / "__pycache__/code.cpython-311.pyc").write_bytes(b"synthetic-bytecode")
    assert snapshots.fingerprint_files({"source": public}) == before
    (public / "code.py").write_text("pass\n")
    assert snapshots.fingerprint_files({"source": public}) != before
    (public / ".env").write_text("SYNTHETIC=only")
    with pytest.raises(snapshots.SnapshotError, match="凭据"):
        snapshots.fingerprint_files({"source": public})


def test_orphan_key_and_directory_in_place_of_database_fail(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(snapshots.SnapshotError, match="没有模型数据库"):
        snapshots.backup(source, tmp_path / "snapshot", quiesced=True, model_key_reference="fixture-v1")
    (source / "voyacart.db").mkdir()
    with pytest.raises(snapshots.SnapshotError, match="固定状态路径"):
        snapshots.backup(source, tmp_path / "snapshot", quiesced=True)


def test_omitted_empty_redis_directory_is_detected(tmp_path):
    source, redis, output = [tmp_path / name for name in ("source", "redis", "snapshot")]
    make_db(source / "voyacart.db").close()
    (redis / "empty").mkdir(parents=True)
    (redis / "dump.rdb").write_bytes(b"synthetic")
    snapshots.backup(source, output, quiesced=True, redis_dir=redis, redis_stopped=True)
    change_manifest(output, lambda m: m.update(directories=[]))
    with pytest.raises(snapshots.SnapshotError, match="目录清单"):
        snapshots.verify(output)


def test_no_encrypted_model_records_cannot_prove_key_continuity(tmp_path):
    source, output = tmp_path / "source", tmp_path / "snapshot"
    model_state(source)
    with sqlite3.connect(source / "models.sqlite3") as db:
        db.execute("DELETE FROM model_secrets")
        db.execute("DELETE FROM model_validation_tickets")
    with pytest.raises(snapshots.SnapshotError, match="可验证"):
        snapshots.backup(source, output, quiesced=True, include_model_key=True)
    assert not output.exists()


def test_invalid_sqlite_does_not_publish_complete_manifest(tmp_path):
    source, output = tmp_path / "source", tmp_path / "snapshot"
    source.mkdir()
    (source / "voyacart.db").write_bytes(b"synthetic-invalid-sqlite")
    with pytest.raises(snapshots.SnapshotError, match="SQLite"):
        snapshots.backup(source, output, quiesced=True)
    assert (output / snapshots.INCOMPLETE).exists()
    assert not (output / snapshots.MANIFEST).exists()


@pytest.mark.parametrize("kind", ["fifo", "hardlink"])
def test_manifest_special_file_rejected_before_read(tmp_path, monkeypatch, kind):
    _, output = small_snapshot(tmp_path)
    manifest = output / snapshots.MANIFEST
    if kind == "fifo":
        manifest.unlink()
        os.mkfifo(manifest)
    else:
        os.link(manifest, tmp_path / "linked-manifest")

    def must_not_read(*_args, **_kwargs):
        pytest.fail("non-regular manifest must be rejected before reading")

    monkeypatch.setattr(Path, "read_text", must_not_read)
    with pytest.raises(snapshots.SnapshotError, match="普通单链接"):
        snapshots.verify(output)
