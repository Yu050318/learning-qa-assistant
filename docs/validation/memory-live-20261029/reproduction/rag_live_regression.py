"""Preserve the existing 80-case quality gates; only allowlisted synthetic files."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
import json
import mimetypes
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

from app.composition.container import Services
from app.core.logging import configure_logging
from app.entity.do.models import User
from app.entity.evaluation.evaluation import AnswerCase, evaluate
from app.entity.evaluation.evaluation_gates import EvaluationGatePolicy, evaluate_gates
from scripts.evaluate_answers import predict_live, judge_prediction
from scripts.verify_enterprise_fixture import verify
from tmp.memory_live_runtime import settings, LOCAL


def main():
    configure_logging()
    corpus=Path('evals/rag/synthetic_enterprise')
    verify(corpus)
    manifest=json.loads((corpus/'manifest.json').read_text(encoding='utf-8'))
    raw=json.loads((corpus/'dataset.json').read_text(encoding='utf-8'))
    output=LOCAL/'rag-regression'
    output.mkdir(exist_ok=False)
    services=Services(settings())
    uid=uuid4()
    try:
        with services.database.sessions() as session:
            session.add(User(id=uid,username='memory-eval-rag-'+uid.hex[:8],password_hash='unused-isolated-fixture'))
            session.commit()
        documents=[]
        ingestion_manifest={'user_id':str(uid),'fixture_fingerprint':manifest['dataset_fingerprint'],
                            'allowlist':manifest['ingestion_allowlist'],'documents':[]}
        (output/'ingestion-manifest.json').write_text(json.dumps(ingestion_manifest,indent=2)+'\n',encoding='utf-8')
        for name in manifest['ingestion_allowlist']:
            path=corpus/name
            row=services.ingestion.upload(uid,path.name,mimetypes.guess_type(path.name)[0],BytesIO(path.read_bytes()))
            services.ingestion.process(uid,row.id,raise_errors=True)
            documents.append(row.id)
            ingestion_manifest['documents'].append({'file':name,'document_id':str(row.id),'status':'ready'})
            (output/'ingestion-manifest.json').write_text(json.dumps(ingestion_manifest,indent=2)+'\n',encoding='utf-8')
            print(json.dumps({'stage':'ingested','file':path.name}),flush=True)
        cases=[AnswerCase.model_validate({**{k:v for k,v in item.items() if k in AnswerCase.model_fields},
                    'user_id':str(uid),'document_ids':[str(did) for did in documents]}) for item in raw['cases']]
        predictions,reviews,events=[],[],[]
        lock=Lock()
        def task(case):
            prediction=predict_live(case,services)
            review,event=None,None
            if not prediction.error_code:
                try:
                    review,event=judge_prediction(case,prediction,services.settings,services.models,'deepseek')
                    event={'id':case.id,'status':'ok',**event}
                except Exception as error:
                    event={'id':case.id,'status':'failed','error_code':type(error).__name__}
            with lock:
                predictions.append(prediction)
                if review: reviews.append(review)
                if event: events.append(event)
                for name,value in [('predictions',[p.model_dump(mode='json') for p in predictions]),
                                   ('reviews',[r.model_dump(mode='json') for r in reviews]),('judge-events',events)]:
                    (output/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                print(json.dumps({'completed':len(predictions),'expected':80,'id':case.id,'error_code':prediction.error_code}),flush=True)
        with ThreadPoolExecutor(max_workers=6) as pool:
            for item in as_completed([pool.submit(task,case) for case in cases]):
                item.result()
        report=evaluate(cases,predictions,reviews)
        policy=EvaluationGatePolicy()
        gate_report={'cases':report['cases'],'execution_failed_cases':report['summary']['failed_runs']}
        report['gate_policy']=policy.model_dump(mode='json')
        report['gate_checks']=evaluate_gates(policy,gate_report,cases,predictions)
        report['gate_passed']=all(c['passed'] for c in report['gate_checks'])
        report['judge_events']=events
        report['comparison']=None
        report['measurement']='80_case_live_regression_current_configuration_not_a_memory_benchmark'
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'stage':'complete','summary':report['summary'],'gate_passed':report['gate_passed']}),flush=True)
    finally:
        services.close()


if __name__=='__main__':
    main()
