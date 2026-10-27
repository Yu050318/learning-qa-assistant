from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.tool_approvals import RunToolApproval


def test_approval_is_bound_to_every_run_identity_field_and_frozen_catalog():
    identity = dict(user_id=uuid4(), session_id=uuid4(), run_id=uuid4(), organization_id=uuid4(), catalog_hash="a" * 64)
    grant = RunToolApproval(**identity, tool_names=("public.lookup", "sandbox_execute"))
    assert grant.allows("public.lookup", **identity)
    assert not grant.allows("other.tool", **identity)
    for name in identity:
        other = "b" * 64 if name == "catalog_hash" else uuid4()
        assert not grant.allows("public.lookup", **{**identity, name: other})
    assert not grant.allows("public.lookup", **{**identity, "organization_id": None})
    assert RunToolApproval.model_validate_json(grant.model_dump_json()) == grant
    assert str(identity["user_id"]) not in repr(grant)


def test_personal_approval_cannot_be_used_in_a_team_or_mutated():
    identity = dict(user_id=uuid4(), session_id=uuid4(), run_id=uuid4(), organization_id=None, catalog_hash="a" * 64)
    grant = RunToolApproval(**identity, tool_names=("sandbox_execute",))
    assert grant.allows("sandbox_execute", **identity)
    assert not grant.allows("sandbox_execute", **{**identity, "organization_id": uuid4()})
    with pytest.raises(ValidationError):
        grant.run_id = uuid4()


@pytest.mark.parametrize("update", [
    {"run_id": None}, {"user_id": "client-supplied-name"}, {"catalog_hash": "unbound"},
    {"tool_names": []}, {"tool_names": ["public.lookup", "public.lookup"]},
    {"tool_names": ["../../shell"]}, {"tool_names": ["UPPER"]}, {"approved": True},
])
def test_invalid_or_ambiguous_approval_is_rejected(update):
    identity = dict(user_id=uuid4(), session_id=uuid4(), run_id=uuid4(), organization_id=None,
        catalog_hash="a" * 64, tool_names=("public.lookup",))
    with pytest.raises(ValidationError):
        RunToolApproval(**{**identity, **update})
