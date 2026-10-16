"""ESCI 新入口的真实检索组件夹具验证，全部外部端口使用本地假实现。"""
from dataclasses import replace
import gzip
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.ports.retrieval_ports import EmbeddingClient, ProductVectorIndex, Reranker, VectorHit
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.persistence.seed_products import _product_from_record
from scripts.esci.prepare import json_line
from scripts.eval import run_esci
from tests.esci_pairing_fixtures import write_fixture_pairing
from scripts.eval.run_esci import STRATEGIES, evaluate_cases, rank_candidates, validate_cases


def fixture_records():
    records=[]
    for index,(title,price,stock,ships,tags) in enumerate([
        ('metal backpack exact',80,5,['CN'],['金属']),
        ('small backpack substitute',100,5,['CN'],['金属']),
        ('backpack cover complement',200,5,['CN'],['合成聚合物']),
        ('office paper irrelevant',10,0,['US'],['纸']),
        ('metal backpack spare unjudged',10,5,['US'],['金属']),
    ]):
        product_id=f'P{100000+index}'
        records.append({'product_id':product_id,'external_product_id':str(index),'canonical_product_id':str(index),
            'title':title,'brand':'Fixture','category':'旅行装备','origin_country':'US','description':'',
            'highlights':[],'source_locale':'us','source_language':'en','source_platform':'amazon',
            'ships_to':ships,'material_tags':tags,'data_provenance':{'source':'constructed_fixture'},
            'skus':[{'sku_id':product_id+'-S1','spec':'default','price_major':price,'currency':'CNY','stock':stock}]})
    return records


def fixture_cases():
    labels={f'P{100000+i}':label for i,label in enumerate(('E','S','C','I'))}
    base={'locale':'us','query_id':1,'query':'metal backpack','category':'办公学习'}
    constraint={**base,'ship_to':'CN','price_max_major':90,'target_currency':'CNY',
        'in_stock':True,'expected_empty':False,'relevant_product_ids':['P100000'],'required_material_tags':['金属']}
    return {'ranking':[{**base,'candidates':[{'product_id':product_id,'label':label} for product_id,label in labels.items()]}],
        'retrieval':[{**base,'labels':labels}],
        'constraints':[constraint,{**constraint,'query_id':2,'price_max_major':0,'expected_empty':True,'relevant_product_ids':[]}],
        'crosslingual':[{**base,'query_id':3,'query':'metal 背包','labels':labels,'original_query':'metal backpack'}]}


class FixtureEmbedding(EmbeddingClient):
    def __init__(self):self.calls=0

    async def embed(self,text):
        self.calls+=1
        return [1.0,0.0]

    async def embed_batch(self,texts):
        self.calls+=1
        return [[1.0,0.0] for _ in texts]


class FixtureIndex(ProductVectorIndex):
    def __init__(self,products):
        self.vectors={p.product_id:vector for p,vector in zip(products,[[1,0],[.8,.6],[0,1],[-1,0],[.6,.8]],strict=True)}

    async def ensure_ready(self,vector_dim):pass

    async def close(self):pass

    async def upsert_products(self,products,embeddings):
        self.vectors.update({p.product_id:v for p,v in zip(products,embeddings,strict=True)})

    async def search(self,embedding,top_n):
        return await self.search_filtered(embedding,top_n,product_ids=list(self.vectors))

    async def search_filtered(self,embedding,top_n,*,product_ids):
        scores=[]
        for product_id in product_ids:
            vector=self.vectors[product_id]
            cosine=sum(a*b for a,b in zip(embedding,vector,strict=True))/math.sqrt(sum(x*x for x in embedding)*sum(x*x for x in vector))
            scores.append(VectorHit(product_id,cosine))
        return sorted(scores,key=lambda hit:(-hit.score,hit.product_id))[:top_n]


class FixtureReranker(Reranker):
    def __init__(self):self.calls=[]

    async def rerank(self,query,documents):
        self.calls.append(list(documents))
        return [1.0 if 'exact' in doc else .1 if 'substitute' in doc else .01 if 'complement' in doc else 0 for doc in documents]


async def test_all_strategies_use_real_search_preserve_constraints_and_share_query_vectors(tmp_path):
    products=[_product_from_record(row) for row in fixture_records()]
    embedder=FixtureEmbedding();reranker=FixtureReranker();report={}
    await evaluate_cases(fixture_cases(),products,STRATEGIES,report,embedder=embedder,vector_index=FixtureIndex(products),reranker=reranker)
    assert report['status']=='COMPLETED' and all(row['status']=='COMPLETED' for row in report['strategies'].values())
    assert len(report['samples'])==6*5 and embedder.calls==1
    assert report['query_encoding']['unique_queries']==2
    assert all(sample['violations']=={} for sample in report['samples'])
    for sample in report['samples']:
        if sample['kind']=='constraints':
            assert sample['retrieved']==(['P100000'] if sample['query_id']==1 else [])
        elif sample['kind']=='ranking':
            assert set(sample['retrieved'])==set(fixture_cases()['retrieval'][0]['labels'])
        else:
            assert 'P100003' not in sample['retrieved']
            assert 'P100000' in sample['retrieved']
    assert any(len(documents)==4 and any('irrelevant' in doc for doc in documents) for documents in reranker.calls)
    assert report['summary']['retrieval']['embedding_only']['us']['metrics']['recall_at_50']==1
    assert report['summary']['constraints']['hybrid_rerank']['all']['constraint_violations']==0
    assert report['summary']['constraints']['hybrid_rerank']['all']['empty_accuracy']==1
    (tmp_path/'fixture-catalog.jsonl').write_text(''.join(json_line(row) for row in fixture_records()))
    (tmp_path/'fixture-cases.json').write_text(json_line(fixture_cases()))
    (tmp_path/'report.json').write_text(json_line(report))
    (tmp_path/'fake-services.json').write_text(json_line({'embedding_batch_calls':embedder.calls,'reranker_documents':reranker.calls,'real_model_calls':0}))


async def test_unconfigured_rerankers_are_not_executed_or_substituted():
    products=[_product_from_record(row) for row in fixture_records()];report={}
    await evaluate_cases(fixture_cases(),products,STRATEGIES,report,embedder=FixtureEmbedding(),vector_index=FixtureIndex(products))
    for name in ('embedding_rerank','hybrid_rerank'):
        assert report['strategies'][name]=={'status':'NOT_EXECUTED','reason':'reranker_not_configured'}
        assert not any(row['strategy']==name for row in report['samples'])
    assert len(report['samples'])==4*5


async def test_bm25_candidate_order_uses_full_corpus_statistics():
    products=[_product_from_record(row) for row in fixture_records()]
    candidates=[replace(products[0],title='red red blue'),replace(products[1],title='blue blue blue')]
    distractors=[replace(products[2],product_id=f'P{200000+i}',title='red') for i in range(20)]
    case={'query':'red blue','candidates':[{'product_id':p.product_id,'label':'E'} for p in candidates]}
    usecase=CatalogSearchUseCase(InMemoryProductRepository(candidates))
    sliced=await rank_candidates(case,'bm25',candidates,usecase,None,None,(1,1))
    full=await rank_candidates(case,'bm25',candidates+distractors,usecase,None,None,(1,1))
    assert sliced==['P100000','P100001']
    assert full==['P100001','P100000']


async def test_vector_failure_is_recorded_and_cannot_publish_keyword_results_as_dense(tmp_path):
    products=[_product_from_record(row) for row in fixture_records()];report={}
    class BrokenIndex(FixtureIndex):
        async def search(self,embedding,top_n):raise ConnectionError('fixture failure')
    with pytest.raises(RuntimeError,match='实际检索路径退化'):
        await evaluate_cases({'retrieval':fixture_cases()['retrieval']},products,('embedding_only',),report,
            embedder=FixtureEmbedding(),vector_index=BrokenIndex(products))
    assert report['status']=='FAILED' and report['strategies']['embedding_only']['status']=='FAILED'
    assert report['samples'][0]['actual_strategy']=='keyword_2gram'
    assert report['samples'][0]['execution_errors']==['requested_vector_strategy_degraded']
    (tmp_path/'expected-failure.json').write_text(json_line(report))


@pytest.mark.parametrize('name',['embedding_rerank','hybrid_rerank'])
async def test_rerank_failure_is_visible_in_requested_strategy(name,tmp_path):
    products=[_product_from_record(row) for row in fixture_records()];report={}
    class BrokenReranker(FixtureReranker):
        async def rerank(self,query,documents):raise ConnectionError('fixture failure')
    with pytest.raises(RuntimeError,match='实际检索路径退化'):
        await evaluate_cases({'retrieval':fixture_cases()['retrieval']},products,(name,),report,
            embedder=FixtureEmbedding(),vector_index=FixtureIndex(products),reranker=BrokenReranker())
    assert report['samples'][0]['execution_errors']==['requested_reranker_not_applied']
    (tmp_path/'expected-failure.json').write_text(json_line(report))


def test_invalid_dataset_references_fail_at_boundary():
    products=[_product_from_record(row) for row in fixture_records()]
    cases=fixture_cases();cases['retrieval'][0]['labels']['unknown']='E'
    with pytest.raises(ValueError,match='目录外'):
        validate_cases(cases,products)
    with pytest.raises(ValueError,match='编号重复'):
        validate_cases(fixture_cases(),products+products[:1])


async def test_exact_identifier_route_is_preserved_and_reported():
    products=[_product_from_record(row) for row in fixture_records()];reranker=FixtureReranker();report={}
    row={**fixture_cases()['retrieval'][0],'query':'P100000','labels':{'P100000':'E'}}
    await evaluate_cases({'retrieval':[row]},products,('embedding_rerank','hybrid_rerank'),report,
        embedder=FixtureEmbedding(),vector_index=FixtureIndex(products),reranker=reranker)
    assert all(sample['actual_strategy']=='exact_id_lookup' and not sample['rerank_applied'] for sample in report['samples'])
    assert all(sample['retrieved']==['P100000'] for sample in report['samples'])
    assert reranker.calls==[]


async def test_development_cli_keeps_holdout_unread_and_writes_source_evidence(tmp_path,monkeypatch):
    catalog=tmp_path/'catalog.jsonl.gz'
    catalog.write_bytes(gzip.compress(''.join(json_line(row) for row in fixture_records()).encode(),mtime=0))
    for kind,rows in fixture_cases().items():
        (tmp_path/f'{kind}-dev.jsonl').write_text(''.join(json_line(row) for row in rows))
    (tmp_path/'crosslingual-zh.jsonl').write_text('fixture trap: derived holdout data must remain unread')
    (tmp_path/'retrieval-holdout.jsonl').write_text('fixture trap: holdout data must remain unread')
    monkeypatch.setattr(run_esci,'load_settings',lambda **kwargs:SimpleNamespace(recall_candidates=32,hybrid_lexical_weight=1,hybrid_vector_weight=1))
    def external_constructor(*args,**kwargs):raise AssertionError('offline strategy attempted external service')
    monkeypatch.setattr(run_esci,'OpenAIEmbeddingClient',external_constructor)
    monkeypatch.setattr(run_esci,'QdrantProductIndex',external_constructor)
    monkeypatch.setattr(run_esci,'create_reranker',external_constructor)
    output=tmp_path/'evaluation'
    manifest_path=write_fixture_pairing(tmp_path,catalog)
    report=await run_esci.run(SimpleNamespace(fixture_manifest=manifest_path,catalog=catalog,dataset_dir=tmp_path,strategies=['keyword_2gram','bm25'],output=output,index_dir=tmp_path/'vectors',profile='dev',frozen=None))
    assert report['status']=='COMPLETED' and report['crosslingual']['status']=='NOT_EXECUTED'
    assert report['holdout']['status']=='NOT_EXECUTED' and report['default_strategy']['status']=='NOT_SELECTED'
    assert report['llm_calls']==0 and report['query_encoding']['unique_queries']==0
    manifest=json.loads((output/'manifest.json').read_text())
    assert manifest['source_stable'] and manifest['data_stable'] and manifest['status']=='COMPLETED'
    assert manifest['parameters']['full_catalog_top_k']==50
    assert all('holdout' not in Path(path).name and 'crosslingual' not in Path(path).name for path in manifest['data_fingerprints'])
    assert (output/'failures.jsonl').exists() and (output/'report.md').exists()
    with pytest.raises(ValueError,match='全部策略'):
        run_esci.freeze_development(output)


@pytest.mark.parametrize('holdout_outcome',['completed','tampered','runtime_failure'])
async def test_frozen_selection_controls_holdout_and_rejects_tampering_before_attempt(tmp_path,monkeypatch,holdout_outcome):
    from tests.test_retrieval import _settings
    catalog=tmp_path/'catalog.jsonl.gz'
    catalog.write_bytes(gzip.compress(''.join(json_line(row) for row in fixture_records()).encode(),mtime=0))
    for kind,rows in fixture_cases().items():
        if kind=='crosslingual':
            (tmp_path/'crosslingual-zh.jsonl').write_text(''.join(json_line(row) for row in rows))
        else:
            for split in ('dev','holdout'):
                (tmp_path/f'{kind}-{split}.jsonl').write_text(''.join(json_line(row) for row in rows))
    products=[_product_from_record(row) for row in fixture_records()]
    settings=replace(_settings(tmp_path),embedding_base_url='http://fixture.invalid/v1',
        embedding_api_key='fixture',embedding_model='fixture-embedding',embedding_dim=2)
    monkeypatch.setattr(run_esci,'load_settings',lambda **kwargs:settings)
    monkeypatch.setattr(run_esci,'OpenAIEmbeddingClient',lambda settings:FixtureEmbedding())
    monkeypatch.setattr(run_esci,'QdrantProductIndex',lambda settings:FixtureIndex(products))
    monkeypatch.setattr(run_esci,'create_reranker',lambda settings:None)
    async def ready(*args,**kwargs):return True
    monkeypatch.setattr(run_esci,'bootstrap_product_index',ready)
    class FixtureLocalEmbedding(FixtureEmbedding):
        model_name='fixture-local';version='fixture-local-v1';dimension=2
        def __init__(self,cache_dir,*,local_files_only):
            assert local_files_only and cache_dir==tmp_path/'local-model'
            super().__init__()
    monkeypatch.setattr('app.infrastructure.embedding.local_multilingual.LocalMultilingualEmbedding',FixtureLocalEmbedding)
    development=tmp_path/'development'
    manifest_path=write_fixture_pairing(tmp_path,catalog)
    common=dict(fixture_manifest=manifest_path,catalog=catalog,dataset_dir=tmp_path,index_dir=tmp_path/'vectors',local_model_dir=tmp_path/'local-model',baseline_version='v2')
    await run_esci.run(SimpleNamespace(**common,strategies=list(STRATEGIES),output=development,profile='dev',frozen=None))
    frozen=run_esci.freeze_development(development)
    assert frozen['strategy']==max(frozen['development_scores'],key=frozen['development_scores'].get)
    assert 'embedding_rerank' not in frozen['development_scores'] and 'hybrid_rerank' not in frozen['development_scores']
    frozen_file=tmp_path/'frozen.json';frozen_file.write_text(json_line(frozen))
    holdout=tmp_path/'holdout'
    changed=replace(settings,hybrid_lexical_weight=2)
    monkeypatch.setattr(run_esci,'load_settings',lambda **kwargs:changed)
    with pytest.raises(ValueError,match='冻结开发清单不同'):
        await run_esci.run(SimpleNamespace(**common,strategies=None,output=tmp_path/'wrong-parameters',profile='holdout',frozen=frozen_file))
    assert not (tmp_path/'baselines/holdout-attempt-v2.json').exists()
    rejected=json.loads((tmp_path/'wrong-parameters/report.json').read_text())
    assert rejected['status']=='FAILED' and rejected['holdout']['status']=='NOT_EXECUTED'
    monkeypatch.setattr(run_esci,'load_settings',lambda **kwargs:settings)
    if holdout_outcome=='tampered':
        (tmp_path/'retrieval-holdout.jsonl').write_text('invalid held-out input')
        with pytest.raises(ValueError,match='配对输入指纹不符'):
            await run_esci.run(SimpleNamespace(**common,strategies=None,output=holdout,profile='holdout',frozen=frozen_file))
        assert not (tmp_path/'baselines/approved-v2.json').exists()
        assert not (tmp_path/'baselines/holdout-attempt-v2.json').exists()
        assert not holdout.exists()
        return
    elif holdout_outcome=='runtime_failure':
        def unavailable_local_model(*args,**kwargs):
            raise RuntimeError('fixture local model unavailable after attempt')
        monkeypatch.setattr('app.infrastructure.embedding.local_multilingual.LocalMultilingualEmbedding',unavailable_local_model)
        with pytest.raises(RuntimeError,match='fixture local model unavailable'):
            await run_esci.run(SimpleNamespace(**common,strategies=None,output=holdout,profile='holdout',frozen=frozen_file))
        assert (tmp_path/'baselines/holdout-attempt-v2.json').exists()
        assert not (tmp_path/'baselines/approved-v2.json').exists()
    else:
        result=await run_esci.run(SimpleNamespace(**common,strategies=None,output=holdout,profile='holdout',frozen=frozen_file))
        assert result['holdout']=={'status':'COMPLETED','attempt':1}
        assert result['crosslingual']['status']=='COMPLETED' and result['llm_calls']==0
        fallback=result['crosslingual_fallback']
        assert {row['actual_strategy'] for row in fallback['samples']}=={'local_embedding','keyword_2gram'}
        assert set(fallback['summary'])=={'local_embedding','keyword_2gram'} and fallback['summary']['local_embedding']['all']['queries']==1
        approved=json.loads((tmp_path/'baselines/approved-v2.json').read_text())
        assert approved['crosslingual_fallback']==fallback['summary']
        assert approved['strategy']==frozen['strategy'] and approved['holdout_attempt']==1
        assert set(result['strategies'])=={frozen['strategy']}
        # 即使 attempt 标记丢失，已批准结果仍阻止再次读取/执行留出。
        attempt_path=tmp_path/'baselines/holdout-attempt-v2.json'
        saved_marker=attempt_path.read_bytes();attempt_path.unlink()
        try:
            with pytest.raises(FileExistsError,match='留出基线已存在'):
                await run_esci.run(SimpleNamespace(**common,strategies=None,
                    output=tmp_path/'approved-only-rejected',profile='holdout',frozen=frozen_file))
            assert not attempt_path.exists()
        finally:
            attempt_path.write_bytes(saved_marker)
    marker=(tmp_path/'baselines/holdout-attempt-v2.json').read_bytes()
    read_rows=run_esci.read_rows
    def protected_read(path):
        assert '-holdout' not in path.name and path.name!='crosslingual-zh.jsonl', '重复尝试读取留出数据'
        return read_rows(path)
    monkeypatch.setattr(run_esci,'read_rows',protected_read)
    with pytest.raises(FileExistsError):
        await run_esci.run(SimpleNamespace(**common,strategies=None,output=tmp_path/'second-attempt',profile='holdout',frozen=frozen_file))
    assert (tmp_path/'baselines/holdout-attempt-v2.json').read_bytes()==marker


@pytest.mark.parametrize('changed_field',[
    {'reranker_protocol':'dashscope'},{'reranker_timeout_seconds':1.0},
])
async def test_frozen_rerank_service_parameters_are_checked_before_holdout(tmp_path,monkeypatch,changed_field):
    from tests.test_retrieval import _settings
    catalog=tmp_path/'catalog.jsonl'
    catalog.write_text(''.join(json_line(row) for row in fixture_records()))
    cases=fixture_cases()
    cases['retrieval'][0]['labels']={'P100000':'I','P100001':'I','P100002':'E','P100003':'I'}
    for kind in ('ranking','retrieval','constraints'):
        (tmp_path/f'{kind}-dev.jsonl').write_text(''.join(json_line(row) for row in cases[kind]))
        (tmp_path/f'{kind}-holdout.jsonl').write_text(''.join(json_line(row) for row in cases[kind]))
    (tmp_path/'crosslingual-zh.jsonl').write_text(''.join(json_line(row) for row in cases['crosslingual']))
    settings=replace(_settings(tmp_path),embedding_base_url='http://fixture.invalid/v1',
        embedding_api_key='fixture',embedding_model='fixture-embedding',embedding_dim=2,
        reranker_base_url='http://fixture.invalid/rerank',reranker_model='fixture-rerank')
    monkeypatch.setattr(run_esci,'load_settings',lambda **kwargs:settings)
    monkeypatch.setattr(run_esci,'OpenAIEmbeddingClient',lambda settings:FixtureEmbedding())
    products=[_product_from_record(row) for row in fixture_records()]
    monkeypatch.setattr(run_esci,'QdrantProductIndex',lambda settings:FixtureIndex(products))
    reranker=FixtureReranker()
    async def prefer_complement(query,documents):
        return [1.0 if 'complement' in document else 0.0 for document in documents]
    reranker.rerank=prefer_complement
    monkeypatch.setattr(run_esci,'create_reranker',lambda settings:reranker)
    async def ready(*args,**kwargs):return True
    monkeypatch.setattr(run_esci,'bootstrap_product_index',ready)
    manifest_path=write_fixture_pairing(tmp_path,catalog)
    common=dict(fixture_manifest=manifest_path,catalog=catalog,dataset_dir=tmp_path,index_dir=tmp_path/'vectors',baseline_version='v2')
    development=tmp_path/'development'
    await run_esci.run(SimpleNamespace(**common,strategies=list(STRATEGIES),output=development,profile='dev',frozen=None))
    frozen=run_esci.freeze_development(development)
    assert frozen['strategy'].endswith('_rerank')
    frozen_file=tmp_path/'frozen.json';frozen_file.write_text(json_line(frozen))
    monkeypatch.setattr(run_esci,'load_settings',lambda **kwargs:replace(settings,**changed_field))
    with pytest.raises(ValueError,match='冻结开发清单不同'):
        await run_esci.run(SimpleNamespace(**common,strategies=None,output=tmp_path/'rejected',profile='holdout',frozen=frozen_file))
    assert not (tmp_path/'baselines/holdout-attempt-v2.json').exists()
