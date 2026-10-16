"""运行来源只在首次 reserve 绑定：真实 SQLite 并发、重放与回滚。"""
import asyncio
import copy
import json
import sqlite3

import pytest

from app.infrastructure.ag_ui_journal import AGUIJournal
from tests.test_ag_ui_journal import body


def binding(**changes):
    return {"P1001-S1": {"product_id": "P1001", "version": 1, "identity_hash": "a" * 64,
                          "terms_hash": "b" * 64, "generation": 1, "active": True, **changes}}


async def catalog_rows(journal):
    async with journal._db() as db:
        return [tuple(row) for row in await (await db.execute("SELECT run_id,snapshot_id FROM agui_run_catalog ORDER BY run_id")).fetchall()]


async def test_new_run_bound_once_old_run_replay_never_retroactively_signed(tmp_path):
    journal = AGUIJournal(tmp_path / "runs.db")
    await journal.reserve(body(), "b1", "old")
    await journal.end_owned("r1", "old", stopped=True)
    identifier = await journal.bind_catalog(binding())
    _, created = await journal.reserve(body(), "b1", "replay")
    assert not created and await catalog_rows(journal) == []
    await journal.reserve(body("r2"), "b1", "fresh")
    assert await catalog_rows(journal) == [("r2", identifier)]
    await journal.end_owned("r2", "fresh", stopped=True)
    newer = await journal.bind_catalog(binding(generation=2))
    assert newer != identifier
    await journal.reserve(body("r2"), "b1", "replay")
    assert await catalog_rows(journal) == [("r2", identifier)]
    await journal.reserve(body("r3"), "b1", "latest")
    assert await catalog_rows(journal) == [("r2", identifier), ("r3", newer)]


async def test_independent_journals_deduplicate_snapshot_and_keep_actual_winner_binding(tmp_path):
    path = tmp_path / "runs.db"
    first, second = AGUIJournal(path), AGUIJournal(path)
    ids = await asyncio.gather(first.bind_catalog(binding()), second.bind_catalog(binding()))
    assert ids[0] == ids[1]
    entries = await asyncio.gather(first.reserve(body(), "b1", "a"), second.reserve(body(), "b1", "b"))
    assert sum(created for _, created in entries) == 1
    assert await catalog_rows(first) == [("r1", ids[0])]
    async with first._db() as db:
        assert (await first._one(db, "SELECT COUNT(*) n FROM agui_catalog_snapshots"))["n"] == 1
    restored = AGUIJournal(path)
    # 未配置当前目录的读取进程可恢复原绑定，不会重签。
    await restored.reserve(body(), "b1", "restored")
    assert await catalog_rows(restored) == [("r1", ids[0])]


async def test_binding_copied_and_reserve_failure_rolls_back_reference_and_run(tmp_path):
    journal = AGUIJournal(tmp_path / "runs.db")
    values = binding()
    identifier = await journal.bind_catalog(values)
    values["P1001-S1"]["generation"] = 999
    async with journal._db(True) as db:
        await db.execute("CREATE TRIGGER reject_session BEFORE INSERT ON agui_sessions BEGIN SELECT RAISE(ABORT, 'fixture rollback'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await journal.reserve(body(), "b1", "owner")
    assert await catalog_rows(journal) == []
    async with journal._db() as db:
        assert (await journal._one(db, "SELECT COUNT(*) n FROM agui_runs"))["n"] == 0
        row = await journal._one(db, "SELECT bindings_json FROM agui_catalog_snapshots WHERE snapshot_id=?", (identifier,))
        assert json.loads(row["bindings_json"])["P1001-S1"]["generation"] == 1


@pytest.mark.parametrize("changes", [{"version": True}, {"version": 2}, {"generation": 0}, {"generation": True},
    {"active": 1}, {"identity_hash": "wrong"}, {"terms_hash": "C" * 64}, {"product_id": ""}, {"extra": "private"}])
async def test_invalid_or_unknown_binding_is_not_saved(tmp_path, changes):
    journal = AGUIJournal(tmp_path / "runs.db")
    with pytest.raises(ValueError):
        await journal.bind_catalog(binding(**changes))
    assert journal._catalog_snapshot_id is None
