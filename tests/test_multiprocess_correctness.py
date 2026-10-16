"""独立 Python 进程共享 SQLite，验证工具历史与整理操作互斥。"""
import asyncio
import json
from pathlib import Path
import sys

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.application.harness.assertions import SequencingTracker
from app.infrastructure.persistence.sql.session_store import SqlFencedSessionStore
from app.infrastructure.persistence.json_file_stores import JsonFileSessionStore
from tests.test_queue_reliability import eventually


@pytest.mark.parametrize('file_mode', [False, True])
async def test_persistent_tool_sequence_survives_process_change(tmp_path, file_mode):
    script = '''
import asyncio,sys
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine
from app.application.harness.assertions import SequencingTracker
from app.infrastructure.persistence.sql.session_store import SqlFencedSessionStore
from app.infrastructure.persistence.json_file_stores import JsonFileSessionStore
async def main():
    engine=create_async_engine('sqlite+aiosqlite:///'+sys.argv[1]+'/sessions.db')
    store=JsonFileSessionStore(Path(sys.argv[1])) if sys.argv[2]=='file' else SqlFencedSessionStore(engine)
    tracker=SequencingTracker(store=store)
    if sys.argv[3]=='first':
        await tracker.record_async('allowed','product_search_tool')
        await tracker.record_async('allowed','product_search_tool')
        await tracker.record_async('rejected','category_insight_tool')
    else:
        assert not (await tracker.check_async('allowed','create_order_tool')).warnings
        assert (await tracker.check_async('rejected','create_order_tool')).rejected
        assert (await tracker.check_async('new','create_order_tool')).warnings
        assert await store.called_tools('allowed') == ['product_search_tool']
    if sys.argv[2]=='file': await store.close()
    await engine.dispose()
asyncio.run(main())
'''
    for stage in ['first','second']:
        process = await asyncio.create_subprocess_exec(sys.executable,'-c',script,str(tmp_path),
            'file' if file_mode else 'sql',stage,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out, error = await process.communicate()
        assert process.returncode == 0, (out+error).decode()


async def test_context_compaction_starts_once_across_two_processes(tmp_path):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path}/sessions.db')
    store = SqlFencedSessionStore(engine)
    await store.assert_owner('s1','b1',create=True)
    await engine.dispose()
    script = '''
import asyncio,json,sys
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy.ext.asyncio import create_async_engine
from app.application.agents.context_service import ContextService
from app.domain.session.ports.session_store import StaleSessionWrite
from app.infrastructure.persistence.sql.session_store import SqlFencedSessionStore
root=Path(sys.argv[1]); identifier=sys.argv[2]
class Service(ContextService):
    async def _run(self,operation,buyer,lock):
        with (root/'executed').open('a') as output: output.write(identifier+'\\n')
        while not (root/'release').exists(): await asyncio.sleep(.01)
        await self.store.finish_context_operation(operation['operation_id'],buyer,'completed',{})
        lock.release()
async def main():
    engine=create_async_engine('sqlite+aiosqlite:///'+str(root/'sessions.db'))
    service=Service(SimpleNamespace(_session_locks={}),SqlFencedSessionStore(engine),None)
    (root/('ready-'+identifier)).touch()
    while not (root/'start').exists(): await asyncio.sleep(.01)
    try:
        operation=await service.start('s1','b1',identifier,0)
        result=operation['status']
    except StaleSessionWrite:
        result='conflict'
    (root/('result-'+identifier)).write_text(result)
    await asyncio.gather(*service.tasks.values())
    await engine.dispose()
asyncio.run(main())
'''
    processes = [await asyncio.create_subprocess_exec(sys.executable,'-c',script,str(tmp_path),str(index),
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE) for index in range(2)]
    try:
        await eventually(lambda: all((tmp_path/f'ready-{i}').exists() for i in range(2)),timeout=15)
        (tmp_path/'start').touch()
        await eventually(lambda: all((tmp_path/f'result-{i}').exists() for i in range(2)))
        assert sorted((tmp_path/f'result-{i}').read_text() for i in range(2)) == ['conflict','running']
        assert len((tmp_path/'executed').read_text().splitlines()) == 1
    finally:
        (tmp_path/'release').touch()
        for process in processes:
            out,error = await process.communicate()
            assert process.returncode == 0, (out+error).decode()
