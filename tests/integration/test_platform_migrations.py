"""Opt-in PostgreSQL migrations; every test uses its own disposable database."""
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.migrate import upgrade
from app.infrastructure.persistence.models import Base

pytestmark = pytest.mark.skipif(os.getenv("RUN_PLATFORM_MIGRATION_INTEGRATION") != "1", reason="requires isolated PostgreSQL")


@pytest.fixture
def database():
    raw = os.getenv("PLATFORM_MIGRATION_TEST_DATABASE_URL", "")
    url = make_url(raw)
    if "test" not in (url.database or "").lower():
        pytest.fail("PLATFORM_MIGRATION_TEST_DATABASE_URL must target a test database")
    name = "parity_test_" + uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url.set(database=name), hide_parameters=True)
    try:
        yield SimpleNamespace(engine=engine)
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def test_empty_upgrade_and_repeat_are_idempotent(database):
    upgrade(database)
    upgrade(database)
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        assert "role" in {c["name"] for c in inspect(connection).get_columns("users", schema="rag")}
        assert set(inspect(connection).get_table_names(schema="rag")) == {table.name for table in Base.metadata.tables.values()}


def test_unversioned_legacy_adoption_preserves_accounts_and_adds_optional_tables(database):
    upgrade(database, "0001_baseline")
    user_id = uuid4()
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO rag.users (id,username,password_hash,is_active,created_at,updated_at) VALUES (:id,'legacy','unchanged-hash',true,now(),now())"), {"id": user_id})
        # Simulate the old bootstrap layout with no Alembic stamp and optional modules absent.
        connection.execute(text("DROP TABLE alembic_version"))
        connection.execute(text("DROP TABLE rag.model_configuration"))
    upgrade(database)
    with database.engine.connect() as connection:
        row = connection.execute(text("SELECT id,password_hash,role FROM rag.users")).one()
        assert row == (user_id, "unchanged-hash", "user")
        assert inspect(connection).has_table("model_configuration", schema="rag")


@pytest.mark.parametrize("change", [
    "ALTER TABLE rag.users DROP COLUMN password_hash",
    "ALTER TABLE rag.users ALTER COLUMN username TYPE varchar(20)",
    "ALTER TABLE rag.documents DROP CONSTRAINT documents_status_check",
    "DROP INDEX rag.ix_documents_user_status; CREATE INDEX ix_documents_user_status ON rag.documents(status)",
])
def test_incompatible_legacy_schema_is_not_stamped(database, change):
    upgrade(database, "0001_baseline")
    with database.engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
        connection.exec_driver_sql(change)
    with pytest.raises(RuntimeError, match="Baseline mismatch"):
        upgrade(database)
    with database.engine.connect() as connection:
        assert not inspect(connection).has_table("alembic_version")
        assert "role" not in {c["name"] for c in inspect(connection).get_columns("users", schema="rag")}


def test_role_constraint_and_destructive_downgrade_are_rejected(database):
    from sqlalchemy.exc import IntegrityError
    upgrade(database)
    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.execute(text("INSERT INTO rag.users (id,username,password_hash,is_active,created_at,updated_at,role) VALUES (:id,'invalid','hash',true,now(),now(),'superuser')"), {"id": uuid4()})
    config = Config("alembic.ini")
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        with pytest.raises(RuntimeError, match="restore a verified backup"):
            command.downgrade(config, "0001_baseline")


def test_worker_heartbeat_expiry_and_recovery_control_http_readiness(database):
    from datetime import timedelta
    from fastapi.testclient import TestClient
    from app.application.container import Services
    from app.application.workers import WorkerRegistry
    from app.core.config import Settings
    from app.infrastructure.persistence.database import Database
    from app.infrastructure.persistence.models import WorkerHeartbeat
    from app.main import create_app

    upgrade(database)
    settings = Settings(_env_file=None, ingestion_mode="durable", jwt_secret="x" * 32,
                        deepseek_api_key="fixture", dashscope_api_key="fixture")
    db = Database(settings)
    db.__dict__["engine"] = database.engine
    registry = WorkerRegistry(settings, db)
    services = SimpleNamespace(settings=settings, database=db, vectors=SimpleNamespace(check=lambda: None),
                               model_center=None, close=lambda: None)
    services.readiness = lambda: Services.readiness(services)
    with TestClient(create_app(settings, services)) as client:
        assert client.get("/health/ready").status_code == 503
        registry.beat()
        assert client.get("/health/ready").status_code == 200
        with db.sessions() as session:
            session.get(WorkerHeartbeat, registry.worker_id).seen_at = registry.now(session) - timedelta(seconds=46)
            session.commit()
        assert client.get("/health/ready").status_code == 503
        registry.beat()
        assert client.get("/health/ready").status_code == 200
    registry.stop()


def test_run_upgrade_interrupts_only_legacy_active_execution_and_preserves_history(database):
    upgrade(database, "0003_worker_readiness")
    uid, sid, running, completed, message = [uuid4() for _ in range(5)]
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO rag.users (id,username,password_hash,is_active,created_at,updated_at) VALUES (:id,'legacy-run','hash',true,now(),now())"), {"id": uid})
        connection.execute(text("INSERT INTO rag.chat_sessions (id,user_id,title,summary,created_at,updated_at) VALUES (:id,:uid,'fixture','',now(),now())"), {"id": sid, "uid": uid})
        connection.execute(text("INSERT INTO rag.chat_messages (id,user_id,session_id,role,content,created_at) VALUES (:id,:uid,:sid,'assistant','preserved answer',now())"), {"id": message, "uid": uid, "sid": sid})
        for rid, state, mid in [(running, "running", None), (completed, "succeeded", message)]:
            connection.execute(text("""INSERT INTO rag.chat_runs
                (id,user_id,session_id,client_key,fingerprint,state,sequence,event_bytes,request_id,expires_at,message_id,created_at)
                VALUES (:id,:uid,:sid,:key,:fingerprint,:state,1,64,'legacy',now()+interval '1 hour',:mid,now())"""),
                {"id": rid, "uid": uid, "sid": sid, "key": uuid4(), "fingerprint": "0" * 64, "state": state, "mid": mid})
            connection.execute(text("INSERT INTO rag.chat_run_events (run_id,sequence,event,payload) VALUES (:id,1,'run_started','{}')"), {"id": rid})
    upgrade(database)
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT state,sequence FROM rag.chat_runs WHERE id=:id"), {"id": running}).one() == ("interrupted", 2)
        assert connection.execute(text("SELECT state,sequence,message_id FROM rag.chat_runs WHERE id=:id"), {"id": completed}).one() == ("succeeded", 1, message)
        assert connection.scalar(text("SELECT content FROM rag.chat_messages WHERE id=:id"), {"id": message}) == "preserved answer"
        assert connection.scalar(text("SELECT payload->>'code' FROM rag.chat_run_events WHERE run_id=:id AND sequence=2"), {"id": running}) == "RUN_UPGRADED_WITHOUT_CHECKPOINT"


def test_event_outbox_notifies_only_after_journal_commit(database):
    import asyncio
    import json
    from app.application.run_events import RunNotifications
    from app.application.runs import RunStore
    from app.core.config import Settings
    from app.domain.contracts import AgentChatInput
    from app.infrastructure.persistence.database import Database
    from app.infrastructure.persistence.models import ChatSession, User

    upgrade(database)
    settings = Settings(_env_file=None)
    db = Database(settings)
    db.__dict__["engine"] = database.engine
    uid, sid = uuid4(), uuid4()
    with db.sessions() as session:
        session.add(User(id=uid, username="outbox-fixture", password_hash="fixture"))
        session.flush()
        session.add(ChatSession(id=sid, user_id=uid, title="fixture"))
        session.commit()
    listener = database.engine.raw_connection()
    try:
        raw = listener.driver_connection
        raw.autocommit = True
        raw.execute("LISTEN rag_run_events")
        store = RunStore(settings, db)
        run, _ = store.create(uid, sid, AgentChatInput("fixture", "sql"), "outbox-fixture", uuid4())
        assert store.dispatch_outbox() == 1
        notices = list(raw.notifies(timeout=2, stop_after=1))
        assert len(notices) == 1
        assert json.loads(notices[0].payload) == {"run_id": str(run.id), "sequence": 1}
        assert store.read(uid, run.id)[1][0].event == "run_started"
        assert store.dispatch_outbox() == 0
        hub = RunNotifications(store)

        async def verify_live_subscription():
            async with hub.subscribe(run.id) as signal:
                # The listener subscribes before draining the persisted outbox.
                store.append(uid, run.id, "answer_start", "")
                hub.start()
                await asyncio.wait_for(signal.wait(), timeout=5)

        try:
            asyncio.run(verify_live_subscription())
        finally:
            hub.close()
        assert hub.subscribers == {} and not hub.thread.is_alive()
        with database.engine.connect() as connection:
            assert not connection.connection.driver_connection.autocommit
    finally:
        listener.invalidate()
        listener.close()


def test_concurrent_run_admission_claim_and_takeover_are_fenced_in_postgresql(database):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from threading import Barrier
    from app.application.chat import ChatService
    from app.application.runs import RunService, RunStore
    from app.application.sessions import SessionService
    from app.core.config import Settings
    from app.core.errors import AppError
    from app.domain.contracts import AgentChatInput
    from app.infrastructure.persistence.database import Database
    from app.infrastructure.persistence.models import ChatMessage, ChatRun, User
    from sqlalchemy import select, func

    upgrade(database)
    settings = Settings(_env_file=None, chat_runs_enabled=True, chat_run_queue_limit=1)
    db = Database(settings)
    db.__dict__["engine"] = database.engine
    with db.sessions() as session:
        user = User(username="concurrent-runs", password_hash="fixture")
        session.add(user)
        session.commit()
    sessions = SessionService(db)
    sid = sessions.create(user.id, "admission").id
    other_sid = sessions.create(user.id, "second").id
    chat = ChatService(settings, db, sessions, None)
    first, second = RunService(settings, db, chat), RunService(settings, db, chat)
    payload, key = AgentChatInput("fixture", "sql"), uuid4()
    try:
        barrier = Barrier(2)

        def create(service):
            barrier.wait(timeout=5)
            return service.start(user.id, sid, payload, "same-key", key)

        with ThreadPoolExecutor(max_workers=2) as executor:
            runs = list(executor.map(create, [first, second]))
        assert runs[0].id == runs[1].id and runs[0].state == runs[1].state == "queued"
        with pytest.raises(AppError) as error:
            second.start(user.id, other_sid, payload, "capacity", uuid4())
        assert error.value.code == "RUN_CAPACITY_EXCEEDED"
        with db.sessions() as session:
            assert session.scalar(select(func.count()).select_from(ChatRun)) == 1
            assert session.scalar(select(func.count()).select_from(ChatMessage)) == 2
        barrier = Barrier(2)

        def claim(store):
            barrier.wait(timeout=5)
            return store.claim_next()

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(claim, [first.store, second.store]))
        assert sum(value is not None for value in claims) == 1
        owner = next(value for value in claims if value is not None)
        first.store.begin_operation(owner, "tool/read", {"query": "frozen"}, readonly=True)
        first.store.complete_operation(owner, "tool/read", {"answer": "cached"})
        with db.sessions() as session:
            run = session.get(ChatRun, owner.run_id)
            run.lease_expires_at = first.store.now(session) - timedelta(seconds=1)
            session.commit()
        takeover = RunStore(settings, db).claim_next()
        assert takeover.generation == owner.generation + 1 and takeover.token != owner.token
        with pytest.raises(AppError) as error:
            first.store.append(user.id, owner.run_id, "answer_delta", "obsolete", claim=owner)
        assert error.value.code == "RUN_LEASE_LOST"
        assert second.store.begin_operation(takeover, "tool/read", {"query": "frozen"}, readonly=True) == (True, {"answer": "cached"})
        second.store.request_cancel(user.id, takeover.run_id)
        second.store.finish_failure(takeover, RuntimeError("fixture"))
        assert second.store.read(user.id, takeover.run_id)[0].state == "cancelled"
    finally:
        first.close()
        second.close()


def test_independent_run_process_reports_only_its_own_live_heartbeat(database, tmp_path):
    import subprocess
    import sys
    from time import monotonic, sleep
    from app.application.workers import WorkerRegistry
    from app.core.config import Settings, PROJECT_ROOT
    from app.infrastructure.persistence.database import Database

    upgrade(database)
    settings = Settings(_env_file=None, chat_runs_enabled=True)
    db = Database(settings)
    db.__dict__["engine"] = database.engine
    marker = tmp_path / "run-worker-id"
    # Inject isolated Settings into the actual entry point, so no workstation
    # dotenv credentials, models or business database are used by this process.
    code = """import os,sys
from app.core.config import Settings
import app.run_worker as worker
worker.get_settings=lambda:Settings(_env_file=None,database_url=os.environ['ISOLATED_RUN_URL'],database_require_tls=False,chat_runs_enabled=True,chat_run_workers=1,jwt_secret='isolated-test-jwt-secret-32-bytes-long')
sys.argv=['run-worker',*sys.argv[1:]]
raise SystemExit(worker.main())
"""
    environment = {**os.environ, "ISOLATED_RUN_URL": database.engine.url.update_query_dict({"sslmode": "disable"}).render_as_string(hide_password=False),
                   "WORKER_HEARTBEAT_FILE": str(marker), "PYTHON_DOTENV_DISABLED": "1", "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        deadline = monotonic() + 20
        while process.poll() is None and not marker.exists() and monotonic() < deadline:
            sleep(.1)
        assert process.poll() is None and marker.exists(), process.communicate(timeout=5)[1] if process.poll() is not None else "worker heartbeat not created"
        assert WorkerRegistry(settings, db, kind="run").healthy_count() == 1
        health = subprocess.run([sys.executable, "-c", code, "--healthcheck"], cwd=PROJECT_ROOT, env=environment,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert health.returncode == 0, health.stderr
        marker.write_text(str(uuid4()))
        wrong = subprocess.run([sys.executable, "-c", code, "--healthcheck"], cwd=PROJECT_ROOT, env=environment,
                               capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert wrong.returncode == 1
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=15)


def test_model_history_upgrade_preserves_current_legacy_revision_and_configuration(database):
    import json
    upgrade(database, "0004_durable_run_control")
    configuration = {"profiles": {"deepseek": {"model_name": "legacy-answer", "enabled": True}}, "legacy_fixture": True}
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO rag.model_configuration(id,revision,configuration,health,updated_at) VALUES (1,7,CAST(:value AS jsonb),'{}',now())"),
                           {"value": json.dumps(configuration)})
    upgrade(database)
    upgrade(database)
    with database.engine.connect() as connection:
        history = connection.execute(text("SELECT revision,configuration,created_by FROM rag.model_configuration_versions")).one()
        assert history == (7, configuration, None)
        assert connection.execute(text("SELECT revision,configuration FROM rag.model_configuration")).one() == (7, configuration)


def test_catalog_concurrency_and_retired_identity_use_real_postgresql_history(database):
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy
    from threading import Barrier
    from app.application.model_center import ModelCenter
    from app.core.config import Settings
    from app.core.errors import AppError
    from app.domain.model_catalog import ModelCatalog
    from app.infrastructure.persistence.database import Database
    from app.infrastructure.persistence.models import User

    upgrade(database)
    settings = Settings(_env_file=None, model_center_enabled=True)
    db = Database(settings)
    db.__dict__["engine"] = database.engine
    with db.sessions() as session:
        user = User(username="catalog-admin", password_hash="fixture", role="admin")
        session.add(user)
        session.commit()
    center = ModelCenter(settings, db)
    center.initialize()
    base = center.catalog_view(user.id)["catalog"]
    base["profiles"]["fast-fixture"] = {**base["profiles"]["ollama"], "id": "fast-fixture", "model_name": "fast-fixture"}
    base["assignments"]["fast"] = "fast-fixture"
    barrier = Barrier(2)

    def update(label):
        data = deepcopy(base)
        data["profiles"]["fast-fixture"]["display_name"] = label
        barrier.wait(timeout=5)
        try:
            return ModelCenter(settings, db).update_catalog(user.id, 1, ModelCatalog.model_validate(data))
        except AppError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, ["first", "second"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "MODEL_CONFIGURATION_CHANGED" in results
    assert center.history(user.id)["total"] == 2
    current = center.catalog_view(user.id)["catalog"]
    retired = deepcopy(current["profiles"]["fast-fixture"])
    current["assignments"]["fast"] = "ollama"
    del current["profiles"]["fast-fixture"]
    center.update_catalog(user.id, 2, ModelCatalog.model_validate(current))
    current["profiles"]["fast-fixture"] = retired
    with pytest.raises(AppError) as error:
        center.update_catalog(user.id, 3, ModelCatalog.model_validate(current))
    assert error.value.code == "MODEL_PROFILE_RETIRED" and center.history(user.id)["total"] == 3


def test_model_call_upgrade_preserves_legacy_rows_and_enforces_complete_profile_identity(database):
    from sqlalchemy.exc import IntegrityError

    upgrade(database, "0004_durable_run_control")
    uid, sid, call_id = uuid4(), uuid4(), uuid4()
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO rag.users(id,username,password_hash,is_active,role,created_at,updated_at) VALUES (:uid,'ledger-fixture','fixture',true,'user',now(),now())"), {"uid": uid})
        connection.execute(text("INSERT INTO rag.chat_sessions(id,user_id,title,summary,created_at,updated_at) VALUES (:sid,:uid,'fixture','',now(),now())"), {"sid": sid, "uid": uid})
        connection.execute(text("""INSERT INTO rag.model_calls
            (id,user_id,session_id,request_id,operation,attempt_index,configuration_revision,provider,model_name,status,fallback,input_tokens,total_tokens,created_at)
            VALUES (:id,:uid,:sid,'legacy','agent',1,7,'deepseek','legacy-model','succeeded',false,11,15,now())"""),
            {"id": call_id, "uid": uid, "sid": sid})
    upgrade(database)
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT model_name,input_tokens,total_tokens,model_role,model_profile_id,model_profile_version FROM rag.model_calls WHERE id=:id"), {"id": call_id}).one() == ("legacy-model", 11, 15, None, None, None)
    for role, profile, version in [("fast", None, None), (None, "planner", 1), ("unknown", "planner", 1), ("fast", "planner", 0)]:
        with pytest.raises(IntegrityError):
            with database.engine.begin() as connection:
                connection.execute(text("UPDATE rag.model_calls SET model_role=:role,model_profile_id=:profile,model_profile_version=:version WHERE id=:id"),
                    {"role": role, "profile": profile, "version": version, "id": call_id})
    with database.engine.begin() as connection:
        connection.execute(text("UPDATE rag.model_calls SET model_role='fast',model_profile_id='planner',model_profile_version=2 WHERE id=:id"), {"id": call_id})


def test_evaluation_job_admission_claim_and_takeover_are_fenced_across_postgres_instances(database):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from threading import Barrier
    from app.application.evaluation_dataset import WorkbenchDataset
    from app.application.evaluation_jobs import EvaluationJobCreate, EvaluationService
    from app.application.model_center import ModelCenter
    from app.core.config import Settings
    from app.core.errors import AppError
    from app.infrastructure.persistence.database import Database
    from app.infrastructure.persistence.models import DocumentRecord, EvaluationJob, User

    upgrade(database)
    settings = Settings(_env_file=None, evaluation_enabled=True, model_center_enabled=True, chat_runs_enabled=True, deepseek_api_key="fixture")
    db = Database(settings)
    db.__dict__["engine"] = database.engine
    with db.sessions() as session:
        user = User(username="evaluation-admin", role="admin", password_hash="fixture")
        session.add(user)
        session.flush()
        doc = DocumentRecord(user_id=user.id, original_name="fixture.txt", file_type=".txt", file_path="fixture",
            content_hash="f" * 64, status="ready", embedding_model=settings.embedding_model, chunk_count=1)
        session.add(doc)
        session.commit()
    ModelCenter(settings, db).initialize()
    service = EvaluationService(settings, db)
    dataset = service.create_dataset(user.id, WorkbenchDataset.model_validate({"name": "fixture", "cases": [
        {"id": "one", "category": "fixture", "question": "fixture?", "expected_kinds": ["insufficient"]}]}))
    payload = EvaluationJobCreate(dataset_id=dataset.id, document_ids=[doc.id])
    key, barrier = uuid4(), Barrier(2)

    def enqueue(_):
        barrier.wait(timeout=5)
        return EvaluationService(settings, db).create_job(user.id, payload, key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(enqueue, [0, 1]))
    assert jobs[0].id == jobs[1].id
    barrier = Barrier(2)

    def claim(_):
        barrier.wait(timeout=5)
        return EvaluationService(settings, db).claim_next()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, [0, 1]))
    assert sum(item is not None for item in claims) == 1
    owner = next(item for item in claims if item is not None)
    with db.sessions() as session:
        session.get(EvaluationJob, owner.job_id).lease_expires_at = service.now(session) - timedelta(seconds=1)
        session.commit()
    takeover = service.claim_next()
    assert takeover.generation == owner.generation + 1 and takeover.token != owner.token
    with pytest.raises(AppError) as failure:
        service.heartbeat(owner)
    assert failure.value.code == "EVALUATION_LEASE_LOST"
    service.cancel(user.id, owner.job_id)
    with pytest.raises(AppError) as failure:
        service.check_owned(takeover)
    assert failure.value.code == "EVALUATION_CANCEL_REQUESTED"


def test_independent_evaluation_process_heartbeat_cannot_impersonate_another_worker(database, tmp_path):
    import subprocess
    import sys
    from time import monotonic, sleep
    from app.application.workers import WorkerRegistry
    from app.core.config import PROJECT_ROOT, Settings
    from app.infrastructure.persistence.database import Database

    upgrade(database)
    settings = Settings(_env_file=None, evaluation_enabled=True, model_center_enabled=True, chat_runs_enabled=True)
    db = Database(settings)
    db.__dict__["engine"] = database.engine
    marker = tmp_path / "evaluation-worker-id"
    code = """import os,sys
from app.core.config import Settings
import app.evaluation_worker as worker
worker.get_settings=lambda:Settings(_env_file=None,database_url=os.environ['ISOLATED_EVALUATION_URL'],database_require_tls=False,evaluation_enabled=True,model_center_enabled=True,chat_runs_enabled=True)
sys.argv=['evaluation-worker',*sys.argv[1:]]
raise SystemExit(worker.main())
"""
    environment = {**os.environ, "ISOLATED_EVALUATION_URL": database.engine.url.update_query_dict({"sslmode": "disable"}).render_as_string(hide_password=False),
        "WORKER_HEARTBEAT_FILE": str(marker), "PYTHON_DOTENV_DISABLED": "1", "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        deadline = monotonic() + 20
        while process.poll() is None and not marker.exists() and monotonic() < deadline:
            sleep(.1)
        assert process.poll() is None and marker.exists(), process.communicate(timeout=5)[1] if process.poll() is not None else "evaluation heartbeat not created"
        assert WorkerRegistry(settings, db, kind="evaluation").healthy_count() == 1
        health = subprocess.run([sys.executable, "-c", code, "--healthcheck"], cwd=PROJECT_ROOT, env=environment,
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert health.returncode == 0, health.stderr
        marker.write_text(str(uuid4()))
        wrong = subprocess.run([sys.executable, "-c", code, "--healthcheck"], cwd=PROJECT_ROOT, env=environment,
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert wrong.returncode == 1
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=15)
