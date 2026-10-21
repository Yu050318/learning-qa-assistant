"""A run-owned call boundary; callers serialize their result and runtime state."""
from app.core.errors import AppError
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Condition
import hashlib
import json
from sqlalchemy import func, select
from app.application.runs import bounded_json
from app.infrastructure.persistence.models import ChatRunOperation


CONTROL_ERRORS = frozenset({"RUN_LEASE_LOST", "RUN_CANCEL_REQUESTED", "AGENT_TIMEOUT", "RUN_CHECKPOINT_DIVERGED",
                           "RUN_CHECKPOINT_LIMIT", "TOOL_OUTCOME_UNCERTAIN", "RUN_RECOVERY_LIMIT",
                           "EVALUATION_LEASE_LOST", "EVALUATION_CANCEL_REQUESTED", "DOCUMENT_SCOPE_CHANGED", "CAPABILITY_SCOPE_CHANGED"})


class RunCheckpoint:
    def __init__(self, store, claim, state=None, validate=None):
        self.store, self.claim = store, claim
        self.state = store.load_checkpoint(claim)[1] if state is None else state
        if "public_inputs" in self.state:
            inputs = self.state["public_inputs"]
            if (not isinstance(inputs, list) or len(inputs) != len(self.state.get("human_inputs", []))
                    or any(value is not None and (not isinstance(value, str) or not 1 <= len(value.strip()) <= 400) for value in inputs)):
                raise AppError("RUN_CHECKPOINT_DIVERGED", "公开补充输入与运行轮次不一致", 409)
        self.frame = ContextVar(f"run-frame-{claim.run_id}", default=f"round/{len(self.state.get('human_inputs', []))}")
        self.validate = validate
        self.ordering = Condition()
        self.finished = {}

    @contextmanager
    def ordered(self, group, index):
        # Preserve the model's tool-call order without restricting the nested
        # graph executor to one thread (which would deadlock child agents).
        while True:
            self.check()
            with self.ordering:
                finished = self.finished.get(group, set())
                if all(previous in finished for previous in range(index)):
                    break
                self.ordering.wait(.2)
        try:
            yield
        finally:
            with self.ordering:
                self.finished.setdefault(group, set()).add(index)
                self.ordering.notify_all()

    @contextmanager
    def scope(self, label):
        token = self.frame.set(self.frame.get() + "/" + label)
        try:
            yield
        finally:
            self.frame.reset(token)

    def key(self, kind, identity, frame=None):
        value = bounded_json({"frame": frame or self.frame.get(), "identity": identity}, 2_000_000)
        digest = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return f"{kind}/{digest}"

    def model_attempts(self):
        with self.store.database.sessions() as session:
            self.store.owned(session, self.claim)
            return session.scalar(select(func.coalesce(func.sum(ChatRunOperation.attempts), 0)).where(
                ChatRunOperation.run_id == self.claim.run_id, ChatRunOperation.operation_key.like("model/%")))

    def check(self):
        self.store.check_owned(self.claim)
        if self.validate is not None:
            self.validate()

    def invoke(self, key, inputs, call, *, readonly):
        self.check()
        cached, outcome = self.store.begin_operation(self.claim, key, inputs, readonly=readonly)
        if not cached:
            self.check()
            try:
                outcome = {"ok": True, "value": call()}
            except AppError as error:
                if error.code in CONTROL_ERRORS:
                    raise
                outcome = {"ok": False, "code": error.code, "message": error.message, "status": error.status_code}
            # Fresh and replayed tool results must have identical JSON ordering:
            # ToolMessage serializes dictionaries into the next model's input.
            outcome = bounded_json(outcome, 2_000_000)
            # Unexpected exceptions leave an unconfirmed receipt. A subsequent
            # owner may retry reads, but cannot repeat unconfirmed side effects.
            self.store.complete_operation(self.claim, key, outcome)
        if not outcome["ok"]:
            raise AppError(outcome["code"], outcome["message"], outcome["status"])
        return outcome["value"]
