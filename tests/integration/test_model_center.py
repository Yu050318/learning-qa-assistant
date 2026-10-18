"""Explicit temporary PostgreSQL: configuration concurrency and call accounting."""
import os
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.application.model_center import ModelCenter
from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import ChatSession, ModelConfiguration, ModelConfigurationVersion, User


@pytest.mark.skipif(os.getenv("RUN_MODEL_CENTER_INTEGRATION") != "1", reason="requires isolated PostgreSQL model-center test DB")
def test_configuration_compare_and_swap_and_metric_lifecycle():
    raw = os.getenv("MODEL_CENTER_TEST_DATABASE_URL", "")
    if not raw or "test" not in (make_url(raw).database or "").lower():
        pytest.fail("MODEL_CENTER_TEST_DATABASE_URL must name an isolated database containing 'test'")
    uid, sid = uuid4(), uuid4()
    username = "models-" + uid.hex[:20]
    settings = Settings(_env_file=None,database_url=raw,model_center_enabled=True,model_admin_usernames=[username])
    db = Database(settings);db.initialize()
    service = ModelCenter(settings,db)
    transaction = db.sessions()
    initialized = False
    try:
        with db.sessions() as session:
            if session.get(ModelConfiguration,1) is not None:
                pytest.fail("model-center integration needs a dedicated test DB with no configuration row")
            session.add(User(id=uid,username=username,password_hash="fixture",role="admin"));session.flush()
            session.add(ChatSession(id=sid,user_id=uid,title="fixture"));session.commit()
        service.initialize();initialized=True
        row = service.row(transaction,lock=True)
        config = service.snapshot().configuration
        with pytest.raises(DBAPIError) as blocked:
            service.update(uid,1,config)
        assert getattr(blocked.value.orig,"sqlstate",None) == "55P03"
        row.revision=2;transaction.commit()
        with pytest.raises(AppError) as stale:
            service.update(uid,1,config)
        assert stale.value.code == "MODEL_CONFIGURATION_CHANGED"
        assert service.update(uid,2,config)["revision"] == 3
        observer = service.observer(uid,sid,"model-center-pg",3)
        handle = observer.start("ollama","fixture",1)
        observer.finish(handle,AIMessage(content="ok",usage_metadata={"input_tokens":2,"output_tokens":1,"total_tokens":3}))
        observer.start("ollama","fixture",2)
        result = service.statistics(uid)["models"][0]
        assert result["attempts"] == 2 and result["reported_calls"] == 1 and result["total_tokens"] is None
        assert result["reported_total_tokens"] == 3 and result["incomplete"] == 1
        service.initialize()
        assert service.snapshot().revision == 3
    finally:
        transaction.rollback();transaction.close()
        with db.sessions() as session:
            session.execute(delete(ChatSession).where(ChatSession.id == sid))
            session.execute(delete(User).where(User.id == uid))
            if initialized:
                session.execute(delete(ModelConfiguration).where(ModelConfiguration.id == 1))
                session.execute(delete(ModelConfigurationVersion))
            session.commit()
        db.close()
