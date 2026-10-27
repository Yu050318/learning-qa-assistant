from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.sandbox import SandboxExecution, SandboxLimits, SandboxRequest


def test_source_is_hidden_and_input_identity_includes_language_and_exact_source():
    request = SandboxRequest(language="python", source="print('PRIVATE_SOURCE')")
    assert "PRIVATE_SOURCE" not in repr(request)
    assert request.input_hash == SandboxRequest.model_validate_json(request.model_dump_json()).input_hash
    assert request.input_hash != SandboxRequest(language="sh", source=request.source).input_hash
    assert request.input_hash != SandboxRequest(language="python", source=request.source + "\n").input_hash


@pytest.mark.parametrize("updates", [
    {"source": " "}, {"source": "x\x00y"}, {"source": "汉" * 22000},
    {"language": "bash"}, {"network": "host"}, {"memory_bytes": 1},
])
def test_model_cannot_supply_runtime_controls_or_unbounded_source(updates):
    with pytest.raises(ValidationError):
        SandboxRequest(**{"language": "python", "source": "print(1)", **updates})


@pytest.mark.parametrize("image", ["runner:latest", "runner:v1", "--privileged", "sha256:" + "a" * 63, "registry/runner@sha256:" + "A" * 64])
def test_execution_rejects_moving_or_ambiguous_images(image):
    with pytest.raises(ValidationError):
        SandboxExecution(invocation_id=uuid4(), run_id=uuid4(), request=SandboxRequest(language="sh", source="printf ok"), image=image, limits=SandboxLimits())


def test_execution_uses_local_or_registry_digest_and_unambiguous_container_identity():
    for image in ("sha256:" + "a" * 64, "registry.example/runner@sha256:" + "a" * 64):
        execution = SandboxExecution(invocation_id=uuid4(), run_id=uuid4(), request=SandboxRequest(language="python", source="print(1)"), image=image, limits=SandboxLimits())
        assert execution.container_name == "learningqa-sandbox-" + execution.invocation_id.hex
        assert SandboxExecution.model_validate_json(execution.model_dump_json()) == execution


@pytest.mark.parametrize("updates", [{"pids_limit": True}, {"timeout_seconds": float('nan')}, {"timeout_seconds": 31.0}, {"cpu_count": 2.0}, {"max_artifact_bytes": 1024}, {"network": "none"}])
def test_server_resource_controls_are_bounded_and_not_extended_by_extra_fields(updates):
    with pytest.raises(ValidationError):
        SandboxLimits(**updates)
