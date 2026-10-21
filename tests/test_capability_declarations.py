"""Registration boundaries; invocation security is tested with its runtime."""
import json

import pytest
from pydantic import ValidationError

from app.domain.capabilities import HttpToolDeclaration, SkillDeclaration


def tool(**updates):
    return HttpToolDeclaration(name="public.lookup", description="Lookup public data", group="public",
                               endpoint="https://api.service.com/v1/lookup", **updates)


def test_declarations_roundtrip_without_secret_values():
    declaration = tool(secret_headers={"Authorization": "PUBLIC_LOOKUP_TOKEN"})
    assert HttpToolDeclaration.model_validate_json(declaration.model_dump_json()) == declaration
    skill = SkillDeclaration(name="public-research", description="Research", instructions="Use cited results.",
                             allowed_tools=(declaration.name,), required_roles=("user",))
    assert SkillDeclaration.model_validate_json(skill.model_dump_json()) == skill
    assert declaration.secret_headers == {"Authorization": "PUBLIC_LOOKUP_TOKEN"}


@pytest.mark.parametrize("endpoint", [
    "http://api.service.com", "https://localhost", "https://a.local", "https://test.internal",
    "https://127.0.0.1", "https://[::1]", "https://169.254.169.254/latest", "https://10.0.0.1",
    "https://user:password@api.service.com", "https://api.service.com:8443", "https://api.service.com/#fragment",
    "https://api.service.com\\@localhost/", "https://api.service.com\n/path", " https://api.service.com",
    "https://a.invalid", "https://[fe80::1%25eth0]", "https://singlelabel", "https://bad..service.com",
])
def test_tool_registration_rejects_unsafe_endpoint(endpoint):
    with pytest.raises(ValidationError):
        HttpToolDeclaration(name="lookup", description="Test", group="public", endpoint=endpoint)


@pytest.mark.parametrize("headers", [
    {"Host": "example.com"}, {"Content-Length": "0"}, {"Cookie": "session=oops"},
    {"X-Api-Key": "literal-secret"}, {"Authorization": "public"}, {"X-Access-Token": "literal"},
    {"X-Test": "ok\r\nInjected: yes"}, {"X-Test": "Basic abc"}, {"X-Test": "x", "x-test": "y"},
    {"X-Test": ""}, {"Connection": "keep-alive"}, {"Bad Header": "value"},
])
def test_headers_cannot_override_transport_or_store_sensitive_values(headers):
    with pytest.raises(ValidationError):
        tool(static_headers=headers)


def test_secret_names_and_overlapping_headers_are_rejected():
    with pytest.raises(ValidationError):
        tool(secret_headers={"Authorization": "Bearer actual-secret"})
    with pytest.raises(ValidationError):
        tool(static_headers={"X-Test": "one"}, secret_headers={"x-test": "TEST_TOKEN"})


@pytest.mark.parametrize("schema", [
    {"type": "array"}, {"type": "object", "properties": []},
    {"type": "object", "$ref": "https://schemas.service.com/schema.json"},
    {"type": "object", "$defs": {"x": {"$dynamicRef": "file:///private"}}},
    {"type": "object", "$id": "https://schemas.service.com/base"},
    {"type": "object", "default": float("nan")},
    {"type": "object", "description": "x" * 65536},
    {"type": "object", "properties": {"query": {"type": "invented-type"}}},
    {"type": "object", "required": "query"},
    {"type": "object", "$schema": "https://schemas.service.com/custom-dialect"},
])
def test_schema_references_and_resource_limits(schema):
    with pytest.raises(ValidationError):
        tool(input_schema=schema)


def test_schema_can_use_local_definitions_but_not_unbounded_structure():
    schema = {"type": "object", "properties": {"query": {"$ref": "#/$defs/query"}},
              "$defs": {"query": {"type": "string", "maxLength": 500}}}
    assert tool(input_schema=schema).input_schema == schema
    deep = {"type": "object"}
    child = deep
    for _ in range(34):
        child["nested"] = {}
        child = child["nested"]
    with pytest.raises(ValidationError):
        tool(input_schema=deep)
    with pytest.raises(ValidationError):
        tool(input_schema={"type": "object", "enum": list(range(4100))})


def test_skill_requirements_cannot_repeat_or_invent_platform_roles():
    common = {"name": "research", "description": "Research", "instructions": "Cite sources"}
    with pytest.raises(ValidationError):
        SkillDeclaration(**common, allowed_tools=("lookup", "lookup"))
    with pytest.raises(ValidationError):
        SkillDeclaration(**common, required_roles=("owner",))
    with pytest.raises(ValidationError):
        SkillDeclaration(**{**common, "instructions": " \n"})
    # JSON clients send arrays; strict decoding retains typed tuples.
    assert SkillDeclaration.model_validate_json(json.dumps({**common, "allowed_tools": ["lookup"]})).allowed_tools == ("lookup",)
