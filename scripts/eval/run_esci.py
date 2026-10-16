"""ESCI 评测：开发选策与参数冻结后，一次执行留出及中文派生集。"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
import gzip
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
from zoneinfo import ZoneInfo

from app.application.usecases.catalog_search import CatalogSearchUseCase, tokenize
from app.domain.catalog.ports.retrieval_ports import EmbeddingClient
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.embedding.openai_embedding_client import OpenAIEmbeddingClient, _MAX_BATCH
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.persistence.seed_products import _product_from_record
from app.infrastructure.rerank.factory import create_reranker
from app.infrastructure.retrieval.bm25 import bm25_rank, reciprocal_rank_fusion
from app.infrastructure.settings import load_settings
from app.infrastructure.vector.index_bootstrap import bootstrap_product_index
from app.infrastructure.vector.qdrant_product_index import QdrantProductIndex
from scripts.esci.download import ROOT, fingerprint
from scripts.esci.prepare import json_line
from scripts.eval.esci_pairing import validate_pairing
from scripts.eval.hard_constraints import find_hit_constraint_violations
from scripts.eval.metrics import ESCI_GAINS, graded_ndcg_at_k, judged_at_k, precision_at_k, recall_at_k

STRATEGIES=('keyword_2gram','bm25','embedding_only','embedding_rerank','hybrid_only','hybrid_rerank')
FROZEN_SCHEMA='esci-frozen-v1'


def evaluation_sources() -> dict:
    paths=sorted((ROOT/'app').rglob('*.py'))+[Path(__file__),ROOT/'scripts/eval/metrics.py',
        ROOT/'scripts/eval/hard_constraints.py',ROOT/'scripts/eval/esci_pairing.py',ROOT/'scripts/esci/download.py',ROOT/'scripts/esci/prepare.py',
        ROOT/'pyproject.toml',ROOT/'uv.lock']
    return {str(path.relative_to(ROOT)):fingerprint(path) for path in paths}


def freeze_development(development: Path) -> dict:
    """依据完整开发报告选策，读取范围限于开发证据和已记录的开发输入。"""
    manifest=json.loads((development/'manifest.json').read_text())
    report=json.loads((development/'report.json').read_text())
    if (manifest['split']!='dev' or report['split']!='dev' or manifest['status']!='COMPLETED'
        or report['status']!='COMPLETED' or not manifest['source_stable'] or not manifest['data_stable']):
        raise ValueError('冻结需要完整且输入稳定的开发报告')
    if manifest['source_fingerprints']!=evaluation_sources():
        raise ValueError('开发评测后源码已变化，需要重新执行开发档')
    if any(fingerprint(Path(name))!=value for name,value in manifest['data_fingerprints'].items()):
        raise ValueError('开发输入已变化，需要重新执行开发档')
    if set(report['strategies'])!=set(STRATEGIES):
        raise ValueError('冻结需要开发档覆盖全部策略，未配置精排时须明确记录')
    scores={}
    for name in STRATEGIES:
        result=report['strategies'][name]
        if result['status']=='NOT_EXECUTED' and name.endswith('_rerank') and result['reason']=='reranker_not_configured':
            continue
        if result['status']!='COMPLETED':
            raise ValueError(f'策略 {name} 未完成，不能冻结')
        value=report['summary']['retrieval'][name]['all']['metrics']['ndcg_at_10']
        if not math.isfinite(value) or not 0<=value<=1:
            raise ValueError('开发 nDCG@10 无效')
        scores[name]=value
    strategy=max(scores,key=scores.get)
    return {'schema_version':FROZEN_SCHEMA,'strategy':strategy,'selection_metric':'retrieval.ndcg_at_10',
        'tie_order':list(STRATEGIES),'development_scores':scores,
        'development':str(development.resolve()),
        'development_fingerprints':{name:fingerprint(development/name) for name in ('manifest.json','report.json')},
        'catalog_fingerprint':manifest['catalog_fingerprint'],'input_pairing':manifest['input_pairing'],
        'parameters':{key:value for key,value in manifest['parameters'].items() if key!='strategies'},
        'embedding':manifest['embedding'],'reranker':manifest['reranker']}


def read_rows(path: Path) -> list[dict]:
    opener=gzip.open if path.suffix=='.gz' else open
    with opener(path,'rt',encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def validate_cases(cases: dict[str,list[dict]], products: list) -> None:
    ids={p.product_id for p in products}
    if not products or len(ids)!=len(products):
        raise ValueError('商品目录为空或编号重复')
    if not cases or any(not rows for rows in cases.values()):
        raise ValueError('ESCI 评测组或查询为空')
    for kind,rows in cases.items():
        if kind not in ('ranking','retrieval','constraints','crosslingual'):
            raise ValueError(f'未知 ESCI 评测组：{kind}')
        seen=set()
        for row in rows:
            key=row['locale'],row['query_id']
            if key in seen or row['locale'] not in ('us','es','jp') or not isinstance(row['query'],str) or not row['query'].strip():
                raise ValueError(f'查询编号、站点或文本无效：{kind}/{key}')
            seen.add(key)
            if kind=='constraints':
                referenced=row['relevant_product_ids']
                ProductSearchSpec(normalized_query=row['query'],ship_to=row['ship_to'],price_max_major=row['price_max_major'],
                    target_currency=row['target_currency'],top_k=5)
                if row['in_stock'] is not True or type(row['expected_empty']) is not bool:
                    raise ValueError('硬约束用例需明确库存与空结果要求')
            else:
                pairs=[(r['product_id'],r['label']) for r in row['candidates']] if kind=='ranking' else list(row['labels'].items())
                referenced=[product_id for product_id,_ in pairs]
                if not pairs or len(set(referenced))!=len(referenced) or any(label not in ESCI_GAINS for _,label in pairs):
                    raise ValueError(f'人工标注重复、为空或等级无效：{kind}/{key}')
            if set(referenced)-ids:
                raise ValueError(f'评测引用了目录外的商品：{kind}/{key}')


class FrozenQueryEmbedding(EmbeddingClient):
    """所有策略复用同一批查询向量，查询编码的耗时单独记录。"""
    def __init__(self, vectors):
        self.vectors=vectors

    async def embed(self, text):
        return self.vectors[text]

    async def embed_batch(self, texts):
        return [self.vectors[text] for text in texts]


class UnavailableEmbedding(EmbeddingClient):
    """远端嵌入服务不可用：检索链依次落到本地多语种（第 3 级）或关键词（第 4 级）。"""
    async def embed(self, text):
        raise ConnectionError('embedding_service_unavailable')

    async def embed_batch(self, texts):
        raise ConnectionError('embedding_service_unavailable')


def local_index_settings(settings, index_dir: Path, model):
    return replace(settings,data_dir=index_dir/'local',qdrant_collection='voyacart_products_esci_eval_local',
        embedding_base_url='local://fastembed',embedding_model=model.model_name,embedding_dim=model.dimension,
        embedding_version=model.version)


async def evaluate_fallback(rows: list[dict], products: list, vector_index, local_embedder, local_index) -> dict:
    """中文派生集：远端嵌入不可用时比较第 3 级本地多语种向量与第 4 级关键词的全库召回。"""
    repo=InMemoryProductRepository(products);result={'samples':[],'summary':{}}
    for expected,local in (('local_embedding',True),('keyword_2gram',False)):
        usecase=CatalogSearchUseCase(repo,UnavailableEmbedding(),vector_index,None,
            local_embedder=local_embedder if local else None,local_vector_index=local_index if local else None)
        for case in rows:
            start=time.perf_counter()
            hits=await usecase.execute(ProductSearchSpec(normalized_query=case['query'],top_k=50,target_currency='CNY'))
            if hits['recall_strategy']!=expected:
                raise RuntimeError(f'兜底实际路径为 {hits["recall_strategy"]}，预期 {expected}')
            retrieved=[hit['product_id'] for hit in hits['hits']]
            result['samples'].append({'kind':'crosslingual_fallback','strategy':expected,'locale':case['locale'],
                'query_id':case['query_id'],'query':case['query'],'retrieved':retrieved,'returned_count':len(retrieved),
                'metrics':recall_metrics(retrieved,case['labels']),'actual_strategy':hits['recall_strategy'],
                'elapsed_ms':(time.perf_counter()-start)*1000})
    grouped=defaultdict(list)
    for row in result['samples']:
        for locale in ('all',row['locale']):grouped[row['strategy'],locale].append(row)
    for (name,locale),group in grouped.items():
        result['summary'].setdefault(name,{})[locale]={'queries':len(group),
            'metrics':{metric:sum(row['metrics'][metric] for row in group)/len(group) for metric in group[0]['metrics']},
            'mean_returned_count':sum(row['returned_count'] for row in group)/len(group),
            'p95_elapsed_ms':sorted(row['elapsed_ms'] for row in group)[math.ceil(.95*len(group))-1]}
    return result


async def rank_candidates(case, strategy, products, usecase, embedder, vector_index, weights):
    """只给人工候选打分；BM25 的文档频率及平均长度取自完整目录。"""
    by_id={p.product_id:p for p in products}
    candidates=[by_id[row['product_id']] for row in case['candidates']]
    spec=ProductSearchSpec(normalized_query=case['query'])
    lexical=[];dense=[]
    if strategy=='keyword_2gram':
        terms=tokenize(case['query'])
        ranked=[(usecase._keyword_score(terms,p,spec),p) for p in candidates]
    else:
        if strategy=='bm25' or strategy.startswith('hybrid'):
            scores={p.product_id:score for score,p in bm25_rank(case['query'],products)}
            lexical=sorted([(scores.get(p.product_id,0.0),p) for p in candidates],key=lambda pair:(-pair[0],pair[1].product_id))
        if strategy not in ('bm25','keyword_2gram'):
            ids=[p.product_id for p in candidates]
            hits=await vector_index.search_filtered(await embedder.embed(case['query']),top_n=len(ids),product_ids=ids)
            if hits is None or len(hits)!=len(ids) or {h.product_id for h in hits}!=set(ids) or any(not math.isfinite(h.score) for h in hits):
                raise RuntimeError('人工候选向量缺失或分数无效，停止排序评测')
            dense=sorted([(h.score,by_id[h.product_id]) for h in hits],key=lambda pair:(-pair[0],pair[1].product_id))
        ranked=lexical if strategy=='bm25' else reciprocal_rank_fusion(lexical,dense,weights=weights) if strategy.startswith('hybrid') else dense
    if strategy.endswith('_rerank'):
        ranked=await usecase._rerank(spec,ranked)
    return [p.product_id for _,p in sorted(ranked,key=lambda pair:(-pair[0],pair[1].product_id))]


def recall_metrics(retrieved: list[str], labels: dict[str,str]) -> dict:
    exact=[product_id for product_id,label in labels.items() if label=='E']
    return {'recall_at_10':recall_at_k(retrieved,exact,10),'recall_at_50':recall_at_k(retrieved,exact,50),
        'ndcg_at_10':graded_ndcg_at_k(retrieved,labels,10),'judged_at_10':judged_at_k(retrieved,labels,10)}


async def evaluate_cases(cases: dict, products: list, strategies: tuple, report: dict, *,
                         embedder=None, vector_index=None, reranker=None, recall_candidates=32, weights=(1.0,1.0)) -> dict:
    validate_cases(cases,products)
    if not strategies or len(set(strategies))!=len(strategies) or set(strategies)-set(STRATEGIES):
        raise ValueError('检索策略为空、重复或无效')
    report.update(status='RUNNING',samples=[],strategies={},query_encoding={'unique_queries':0,'seconds':0.0},llm_calls=0)
    active=[]
    for name in strategies:
        reason='reranker_not_configured' if name.endswith('_rerank') and reranker is None else (
            'embedding_or_index_not_configured' if name not in ('keyword_2gram','bm25') and (embedder is None or vector_index is None) else None)
        report['strategies'][name]={'status':'NOT_EXECUTED','reason':reason} if reason else {'status':'PENDING'}
        if reason is None:active.append(name)
    if any(name not in ('keyword_2gram','bm25') for name in active):
        queries=sorted({row['query'] for rows in cases.values() for row in rows})
        started=time.perf_counter();vectors=await embedder.embed_batch(queries)
        if len(vectors)!=len(queries) or any(not vector or not all(type(x) in (int,float) and math.isfinite(x) for x in vector) for vector in vectors):
            raise ValueError('查询向量数量或内容无效')
        embedder=FrozenQueryEmbedding(dict(zip(queries,vectors,strict=True)))
        report['query_encoding']={'unique_queries':len(queries),'seconds':time.perf_counter()-started}
    repo=InMemoryProductRepository(products);by_id={p.product_id:p for p in products}
    for name in active:
        report['strategies'][name]={'status':'RUNNING'}
        dense=name not in ('keyword_2gram','bm25')
        usecase=CatalogSearchUseCase(repo,embedder if dense else None,vector_index if dense else None,
            reranker if name.endswith('_rerank') else None,hybrid_enabled=name in ('bm25','hybrid_only','hybrid_rerank'),
            hybrid_lexical_weight=weights[0],hybrid_vector_weight=weights[1],recall_candidates=recall_candidates)
        for kind,rows in cases.items():
            for case in rows:
                start=time.perf_counter();errors=[];violations={};empty_ok=None
                if kind=='ranking':
                    retrieved=await rank_candidates(case,name,products,usecase,embedder,vector_index,weights)
                    labels={row['product_id']:row['label'] for row in case['candidates']}
                    metrics={'ndcg':graded_ndcg_at_k(retrieved,labels,len(labels)),
                        'ndcg_at_10':graded_ndcg_at_k(retrieved,labels,10)}
                    actual='candidate_'+name;rerank_applied=name.endswith('_rerank')
                else:
                    constrained=kind=='constraints'
                    spec=ProductSearchSpec(normalized_query=case['query'],top_k=5 if constrained else 50,
                        ship_to=case['ship_to'] if constrained else None,
                        price_max_major=case['price_max_major'] if constrained else None,
                        target_currency=case['target_currency'] if constrained else 'CNY',
                        required_material_tags=case.get('required_material_tags',[]) if constrained else [],
                        excluded_material_tags=case.get('excluded_material_tags',[]) if constrained else [])
                    result=await usecase.execute(spec)
                    retrieved=[hit['product_id'] for hit in result['hits']]
                    actual=result['recall_strategy'];rerank_applied=result['rerank_applied']
                    if dense and actual in ('keyword_2gram','bm25','bm25_rerank','local_embedding'):
                        errors.append('requested_vector_strategy_degraded')
                    if name.endswith('_rerank') and result['hits'] and not rerank_applied and actual!='exact_id_lookup':
                        errors.append('requested_reranker_not_applied')
                    if constrained:
                        empty_ok=not retrieved if case['expected_empty'] else None
                        metrics={'recall_at_5':None if case['expected_empty'] else recall_at_k(retrieved,case['relevant_product_ids'],5),
                            'precision_at_5':None if case['expected_empty'] else precision_at_k(retrieved,case['relevant_product_ids'],5)}
                        violations=find_hit_constraint_violations(result['hits'],by_id,ship_to=spec.ship_to,
                            price_max_major=spec.price_max_major,target_currency=spec.target_currency,
                            required_material_tags=spec.required_material_tags,excluded_material_tags=spec.excluded_material_tags)
                    else:
                        metrics=recall_metrics(retrieved,case['labels'])
                sample={'kind':kind,'strategy':name,'locale':case['locale'],'query_id':case['query_id'],'query':case['query'],
                    'retrieved':retrieved,'returned_count':len(retrieved),'metrics':metrics,'actual_strategy':actual,
                    'rerank_applied':rerank_applied,'elapsed_ms':(time.perf_counter()-start)*1000,
                    'violations':{product_id:sorted(reasons) for product_id,reasons in violations.items()},
                    'empty_ok':empty_ok,'execution_errors':errors}
                report['samples'].append(sample)
                if errors or violations or empty_ok is False:
                    report['strategies'][name]={'status':'FAILED'}
                    report['status']='FAILED'
                    raise RuntimeError('实际检索路径退化或硬约束结果无效，停止该策略评测')
        report['strategies'][name]={'status':'COMPLETED'}
    grouped=defaultdict(list)
    for row in report['samples']:
        for locale in ('all',row['locale']):grouped[row['kind'],row['strategy'],locale].append(row)
    report['summary']={}
    for (kind,name,locale),rows in grouped.items():
        metrics={metric:sum(values)/len(values) for metric in rows[0]['metrics']
            if (values:=[row['metrics'][metric] for row in rows if row['metrics'][metric] is not None])}
        checked=[row['empty_ok'] for row in rows if row['empty_ok'] is not None]
        summary={'queries':len(rows),'metrics':metrics,'mean_returned_count':sum(row['returned_count'] for row in rows)/len(rows),
            'constraint_violations':sum(len(row['violations']) for row in rows),
            'empty_accuracy':sum(checked)/len(checked) if checked else None,
            'actual_strategies':sorted({row['actual_strategy'] for row in rows})}
        report['summary'].setdefault(kind,{}).setdefault(name,{})[locale]=summary
    report['status']='COMPLETED'
    return report


async def run(args) -> dict:
    split=args.profile
    # 最先拒绝错配；此处之前不创建报告、索引、客户端或留出 attempt。
    fixture_manifest=getattr(args,'fixture_manifest',None)
    pairing=validate_pairing(catalog=args.catalog,dataset_dir=args.dataset_dir,profile=split,
        manifest=fixture_manifest or getattr(args,'pairing_manifest',None),
        baseline_version=getattr(args,'baseline_version',None),allow_fixture=fixture_manifest is not None)
    frozen=None
    if split=='holdout':
        if args.frozen is None or args.strategies is not None:
            raise ValueError('留出档须提供冻结清单，策略由清单指定')
        frozen=json.loads(args.frozen.read_text())
        if frozen['schema_version']!=FROZEN_SCHEMA or frozen!=freeze_development(Path(frozen['development'])):
            raise ValueError('冻结清单与完整开发结果不同')
    elif args.frozen is not None:
        raise ValueError('开发档独立运行，冻结清单用于留出档')
    strategies=[frozen['strategy']] if frozen else (args.strategies or ['keyword_2gram','bm25'])
    args.output.mkdir(parents=True,exist_ok=False)
    dataset_paths={kind:args.dataset_dir/f'{kind}-{split}.jsonl' for kind in ('ranking','retrieval','constraints')}
    if split=='holdout':dataset_paths['crosslingual']=args.dataset_dir/'crosslingual-zh.jsonl'
    manifest={'started_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'split':split,'status':'RUNNING',
        'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'fingerprint_method':'blake2b','source_fingerprints':evaluation_sources(),'input_pairing':pairing}
    report={'status':'RUNNING','samples':[],'split':split,'llm_calls':0,
        'crosslingual':{'status':'NOT_EXECUTED','reason':'derived_from_holdout_requires_frozen_parameters'},
        'holdout':{'status':'NOT_EXECUTED'},'default_strategy':{'status':'NOT_SELECTED'},
        'metric_definitions':{'gains':ESCI_GAINS,'judged_at_10':'前10条去重返回结果中有人工标注的比例，分母为实际返回数',
            'category':'查询类目用于报告，全库及硬约束检索不传类目',
            'latency':'复用固定查询向量后的检索耗时，查询编码耗时单独记录',
            'constraints':'应为空用例独立统计空结果准确率，其余用例统计Recall@5、Precision@5',
            'crosslingual_fallback':'仅留出档：中文派生集上远端嵌入调用失败，分别走本地多语种向量（第3级）与keyword_2gram（第4级），不混合、不精排'}}
    vector_index=None
    (args.output/'manifest.json').write_text(json_line(manifest))
    try:
        if not strategies or len(set(strategies))!=len(strategies) or set(strategies)-set(STRATEGIES):
            raise ValueError('检索策略为空、重复或无效')
        settings=load_settings(require_llm_key=False)
        params={'strategies':list(strategies),'recall_candidates':settings.recall_candidates,
            'rrf_weights':[settings.hybrid_lexical_weight,settings.hybrid_vector_weight],'rrf_k':60,
            'full_catalog_top_k':50,'constraints_top_k':5}
        manifest['parameters']=params;report['parameters']=params
        reranker=create_reranker(settings) if any(name.endswith('_rerank') for name in strategies) else None
        enabled=[name for name in strategies if not name.endswith('_rerank') or reranker is not None]
        dense=any(name not in ('keyword_2gram','bm25') for name in enabled)
        manifest['embedding']={'model':settings.embedding_model,'version':settings.embedding_version,
            'dimension':settings.embedding_dim,'collection':'voyacart_products_esci_eval',
            'max_batch':_MAX_BATCH,'timeout_seconds':15.0,
            'gateway_fingerprint':hashlib.blake2b(settings.embedding_base_url.encode()).hexdigest()} if dense else None
        manifest['reranker']={'model':settings.reranker_model,
            'protocol':settings.reranker_protocol,'timeout_seconds':settings.reranker_timeout_seconds,
            'gateway_fingerprint':hashlib.blake2b(settings.reranker_base_url.encode()).hexdigest()} if reranker is not None else None
        manifest['catalog_fingerprint']=fingerprint(args.catalog)
        if dense and (not settings.embedding_api_key or not settings.embedding_base_url or not settings.embedding_model):
            raise RuntimeError('向量策略需要 EMBEDDING_BASE_URL、EMBEDDING_API_KEY 和 EMBEDDING_MODEL')
        if frozen:
            if (frozen['parameters']!={key:value for key,value in params.items() if key!='strategies'}
                or frozen['catalog_fingerprint']!=manifest['catalog_fingerprint']
                or frozen['input_pairing']['manifest_blake2b']!=pairing['manifest_blake2b']
                or (dense and frozen['embedding']!=manifest['embedding'])
                or (reranker is not None and frozen['reranker']!=manifest['reranker'])):
                raise ValueError('留出配置或目录与冻结开发清单不同')
            if strategies[0].endswith('_rerank') and reranker is None:
                raise ValueError('冻结策略的精排服务未配置')
            attempt=args.dataset_dir/f'baselines/holdout-attempt-{args.baseline_version}.json'
            if (attempt.parent/f'approved-{args.baseline_version}.json').exists():
                raise FileExistsError('留出基线已存在，本轮保持首次结果')
            attempt.parent.mkdir(parents=True,exist_ok=True)
            with attempt.open('x') as stream:
                stream.write(json_line({'attempt':1,'started_at':manifest['started_at'],
                    'output':str(args.output.resolve()),'frozen_fingerprint':fingerprint(args.frozen),
                    'frozen_file':str(args.frozen.resolve())}))
            manifest['holdout_attempt']=str(attempt.resolve())
            manifest['frozen_fingerprint']=fingerprint(args.frozen)
            report['default_strategy']={'status':'FROZEN','strategy':frozen['strategy']}
            report['holdout']={'status':'RUNNING','attempt':1}
            report['crosslingual']={'status':'RUNNING'}
            (args.output/'manifest.json').write_text(json_line(manifest))
        data_paths=[Path(pairing['manifest']),*[Path(item['path']) for item in pairing['verified_artifacts'].values()]]
        manifest['data_fingerprints']={str(p.resolve()):fingerprint(p) for p in data_paths}
        products=[_product_from_record(row) for row in read_rows(args.catalog)]
        cases={kind:read_rows(path) for kind,path in dataset_paths.items()}
        validate_cases(cases,products)
        embedder=None
        if dense:
            index_settings=replace(settings,data_dir=args.index_dir,qdrant_collection='voyacart_products_esci_eval')
            embedder=OpenAIEmbeddingClient(index_settings);vector_index=QdrantProductIndex(index_settings)
            report['index_build']={};started=time.perf_counter()
            complete=await bootstrap_product_index(InMemoryProductRepository(products),embedder,vector_index,report=report['index_build'])
            report['index_build']['seconds']=time.perf_counter()-started
            if not complete:raise RuntimeError('ESCI 向量索引未完成，停止评测')
        (args.output/'manifest.json').write_text(json_line(manifest))
        await evaluate_cases(cases,products,tuple(strategies),report,embedder=embedder,vector_index=vector_index,reranker=reranker,
            recall_candidates=settings.recall_candidates,weights=tuple(params['rrf_weights']))
        if frozen:
            from app.infrastructure.embedding.local_multilingual import LocalMultilingualEmbedding
            report['status']='RUNNING'
            local_embedder=await asyncio.to_thread(LocalMultilingualEmbedding,args.local_model_dir,local_files_only=True)
            local_settings=local_index_settings(settings,args.index_dir,local_embedder)
            manifest['local_embedding']={'model':local_embedder.model_name,'version':local_embedder.version,
                'dimension':local_embedder.dimension,'collection':local_settings.qdrant_collection}
            (args.output/'manifest.json').write_text(json_line(manifest))
            local_index=QdrantProductIndex(local_settings)
            try:
                build={};started=time.perf_counter()
                if not await bootstrap_product_index(InMemoryProductRepository(products),local_embedder,local_index,report=build):
                    raise RuntimeError('本地多语种索引未完成，停止兜底评测')
                build['seconds']=time.perf_counter()-started
                report['crosslingual_fallback']={'local_index_build':build,
                    **await evaluate_fallback(cases['crosslingual'],products,vector_index,local_embedder,local_index)}
            finally:
                await local_index.close()
            report['status']='COMPLETED'
        return report
    finally:
        if vector_index is not None:await vector_index.close()
        if report['status']=='RUNNING':report['status']='FAILED'
        for name,result in report.get('strategies',{}).items():
            if result['status']=='RUNNING':report['strategies'][name]={'status':'FAILED'}
            elif result['status']=='PENDING':report['strategies'][name]={'status':'NOT_EXECUTED','reason':'earlier_stage_failed'}
        manifest['source_stable']=all(fingerprint(ROOT/name)==value for name,value in manifest['source_fingerprints'].items())
        manifest['data_stable']='data_fingerprints' in manifest and all(
            fingerprint(Path(name))==value for name,value in manifest['data_fingerprints'].items())
        changed=not manifest['source_stable'] or ('data_fingerprints' in manifest and not manifest['data_stable'])
        manifest['status']='INPUTS_CHANGED' if changed else report['status']
        report['status']=manifest['status']
        if frozen and 'holdout_attempt' in manifest:
            report['holdout']={'status':report['status'],'attempt':1}
            report['crosslingual']={'status':report['status']}
        (args.output/'manifest.json').write_text(json_line(manifest))
        (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n')
        (args.output/'failures.jsonl').write_text(''.join(json_line(row) for row in report['samples']
            if row['execution_errors'] or row['violations'] or row['empty_ok'] is False
            or any(value is not None and value<1 for value in row['metrics'].values())))
        lines=['# ESCI '+('开发' if split=='dev' else '留出')+'档评测','','状态：'+report['status']+'。','',
            '| 组别 | 策略 | 站点 | 查询数 | 指标 |','|---|---|---|---|---|']
        for kind,strategies in report.get('summary',{}).items():
            for name,locales in strategies.items():
                for locale,values in locales.items():
                    lines.append(f'| {kind} | {name} | {locale} | {values["queries"]} | '+', '.join(f'{k}={v:.4f}' for k,v in values['metrics'].items())+' |')
        for name,locales in report.get('crosslingual_fallback',{}).get('summary',{}).items():
            for locale,values in locales.items():
                lines.append(f'| crosslingual_fallback | {name} | {locale} | {values["queries"]} | '+', '.join(f'{k}={v:.4f}' for k,v in values['metrics'].items())+' |')
        for name,result in report.get('strategies',{}).items():
            if result['status']=='NOT_EXECUTED':lines.append(f'\n{name}：未执行，原因 {result["reason"]}。')
        (args.output/'report.md').write_text('\n'.join(lines)+'\n')
        if frozen and report['status']=='COMPLETED':
            approved={'status':'COMPLETED','strategy':frozen['strategy'],'holdout_attempt':1,
                'attempt_file':manifest['holdout_attempt'],'frozen_fingerprint':manifest['frozen_fingerprint'],
                'report':str((args.output/'report.json').resolve()),'report_fingerprint':fingerprint(args.output/'report.json'),
                'manifest':str((args.output/'manifest.json').resolve()),'manifest_fingerprint':fingerprint(args.output/'manifest.json'),
                'summary':report['summary'],'crosslingual_fallback':report['crosslingual_fallback']['summary']}
            with (args.dataset_dir/f'baselines/approved-{args.baseline_version}.json').open('x') as stream:
                stream.write(json_line(approved))


def argument_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',type=Path,default=ROOT/'data/catalog-esci-v2.jsonl.gz')
    parser.add_argument('--dataset-dir',type=Path,default=ROOT/'eval/esci-v2')
    pairing=parser.add_mutually_exclusive_group()
    pairing.add_argument('--pairing-manifest',type=Path,help='成套冻结输入 manifest；默认读取 dataset-dir/input-pairing.json')
    pairing.add_argument('--fixture-manifest',type=Path,help='仅构造夹具：显式独立 fixture manifest，不能用于正式冻结目录')
    parser.add_argument('--strategies',nargs='+',choices=STRATEGIES)
    parser.add_argument('--profile',choices=('dev','holdout'),default='dev')
    parser.add_argument('--frozen',type=Path,help='留出档使用的完整开发参数清单')
    parser.add_argument('--baseline-version',default='v2',help='留出尝试标记与批准基线的版本号，与目录、评测集属于同一轮定稿')
    parser.add_argument('--freeze-from',type=Path,help='从完整开发证据选策并写入 --output 指定的新清单文件')
    parser.add_argument('--index-dir',type=Path,default=ROOT/'data/esci-evaluation-vectors-v2')
    parser.add_argument('--local-model-dir',type=Path,default=ROOT/'data/local_embedding/model',help='留出档兜底对比使用的本地多语种模型缓存')
    parser.add_argument('--output',type=Path,required=True)
    return parser


def main():
    args=argument_parser().parse_args()
    if args.freeze_from:
        frozen=freeze_development(args.freeze_from)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x') as stream:stream.write(json_line(frozen))
        print(json_line({'status':'FROZEN','strategy':frozen['strategy']}).strip())
        return
    report=asyncio.run(run(args))
    print(json_line({'status':report['status'],'strategies':report['strategies']}).strip())
    raise SystemExit(0 if report['status']=='COMPLETED' else 1)


if __name__=='__main__':main()
