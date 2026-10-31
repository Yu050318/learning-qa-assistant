"""Independent isolation/deletion and identical-fact retrieval trials."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import secrets
from threading import Lock
from time import sleep, monotonic
from uuid import UUID, uuid4

import httpx
from app.composition.container import Services
from app.core.logging import configure_logging
from app.entity.evaluation.memory import MemoryDataset
from app.manager.memory_evaluation import MemoryEvaluationClient
from scripts.evaluate_memory_retrieval import run_trial, summarize_trials
from app.manager.openviking import OpenVikingManager
from tmp.memory_live_runtime import settings, LOCAL


class VectorOnlyManager(OpenVikingManager):
    def request(self, user_id, generation, method, path, **kwargs):
        if path == '/api/v1/content/batch-write':
            kwargs['json'] = {**kwargs['json'], 'processing_mode': 'vectors_only'}
        return super().request(user_id, generation, method, path, **kwargs)


def experiment_settings(mode):
    config = settings()
    if mode == 'retrieval':
        config = config.model_copy(update={'openviking_url': 'http://127.0.0.1:19341', 'openviking_worker_timeout': 30})
    return config


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=['isolation','retrieval'],required=True)
    parser.add_argument('--workers',type=int,default=8)
    parser.add_argument('--smoke',action='store_true')
    args=parser.parse_args()
    if args.mode == 'retrieval':
        with httpx.Client(trust_env=False,timeout=2) as probe:
            deadline=monotonic()+120
            while monotonic()<deadline:
                try:
                    if probe.get('http://127.0.0.1:19341/health').is_success: break
                except httpx.HTTPError: pass
                sleep(1)
            else: raise RuntimeError('EMBEDDING_ONLY_SERVER_STARTUP_TIMEOUT')
    configure_logging()
    dataset=MemoryDataset.model_validate_json(Path('evals/memory/personal-v1.json').read_text(encoding='utf-8'))
    cases=[c for c in dataset.cases if c.split=='frozen' and (args.mode=='isolation' or c.gold_memories)]
    if args.smoke: cases=cases[:1]
    report={'dataset_fingerprint':dataset.fingerprint,'split':'frozen','measurement':args.mode,
            'workers':args.workers,'expected_trials':len(cases)*(3 if args.mode=='retrieval' else 1),'cases':[],
            'embedding_usage':None,'indexing_usage':None}
    if args.mode == 'retrieval':
        report.update(index_processing='vectors_only', llm_endpoint='unreachable_dummy', top_k=3)
    output=LOCAL/(args.mode+('-smoke' if args.smoke else '')+'.json')
    assert not output.exists()
    lock=Lock()
    pairs=[]
    fixture_services=Services(experiment_settings(args.mode))
    from app.entity.do.models import User
    with fixture_services.database.sessions() as session:
        for lane in range(args.workers):
            pair=[]
            for side in range(2):
                username='memory-eval-'+args.mode[:1]+str(lane)+str(side)+'-'+uuid4().hex[:8]
                uid=uuid4()
                session.add(User(id=uid,username=username,password_hash='unused-evaluation-login-disabled'))
                pair.append(str(uid))
            pairs.append(pair)
        session.commit()
    fixture_services.close()
    def lane(index):
        services=Services(experiment_settings(args.mode))
        if args.mode == 'retrieval':
            services.openviking.close()
            services.openviking=VectorOnlyManager(services.settings)
        try:
            uid,other=[UUID(value) for value in pairs[index]]
            for position,case in enumerate(cases):
                if position%args.workers!=index: continue
                for trial in range(1,4 if args.mode=='retrieval' else 2):
                    row={'case_id':case.id,'trial':trial,'error_code':None}
                    try:
                        if args.mode=='retrieval':
                            row.update(run_trial(services,uid,other,case))
                        else:
                            clients=[]
                            try:
                                from app.entity.do.models import User
                                for user_id in (uid,other):
                                    with services.database.sessions() as session:
                                        user=session.get(User,user_id)
                                    token,_,_=services.auth._issue_session(user)
                                    clients.append(MemoryEvaluationClient('http://127.0.0.1:18095',token))
                                source,observer=clients
                                source.reset(True); observer.reset(True)
                                canary='测试公司'+uuid4().hex
                                source.ask(source.session(),f'我的工作单位正式名称是“{canary}”，这是我长期任职的公司。')
                                source.settle()
                                memories=source.request('GET','/api/v2/automatic-memories')
                                row['source_verified']=any(canary in item['content'] for item in memories)
                                if not row['source_verified']: raise RuntimeError('SOURCE_CANARY_NOT_EXTRACTED')
                                question='我长期任职的公司正式名称是什么？没有个人记忆依据时请说不知道。'
                                foreign=observer.ask(observer.session(),question)
                                row['cross_user_leak']=(canary in json.dumps(foreign,ensure_ascii=False)
                                    or any(c.get('type')=='memory' for c in foreign.get('citations',[]))
                                    or bool(observer.request('GET','/api/v2/automatic-memories')))
                                for item in memories:
                                    source.request('DELETE','/api/v2/automatic-memories/'+item['id'])
                                source.settle()
                                after=source.ask(source.session(),question)
                                row['deleted_recall']=(canary in json.dumps(after,ensure_ascii=False)
                                    or any(c.get('type')=='memory' for c in after.get('citations',[])))
                            finally:
                                for client in clients: client.close()
                    except Exception as error:
                        row['error_code']=str(error) if type(error) is RuntimeError and str(error).isupper() else type(error).__name__
                    with lock:
                        report['cases'].append(row)
                        if args.mode=='retrieval': report['summary']=summarize_trials(report['cases'])
                        else:
                            report['summary']={'completed':len(report['cases']),
                                'execution_failures':sum(bool(r['error_code']) for r in report['cases']),
                                'source_verified':sum(r.get('source_verified') is True for r in report['cases']),
                                'cross_user_leaks':sum(r.get('cross_user_leak') is True for r in report['cases']),
                                'deleted_recalls':sum(r.get('deleted_recall') is True for r in report['cases'])}
                        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                        print(json.dumps({'mode':args.mode,'completed':len(report['cases']),'expected':report['expected_trials'],
                            'case_id':case.id,'error_code':row['error_code']}),flush=True)
        finally:
            services.close()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for item in as_completed([pool.submit(lane,i) for i in range(args.workers)]): item.result()
    print(json.dumps({'stage':'complete','mode':args.mode,'summary':report['summary']}),flush=True)


if __name__=='__main__': main()
