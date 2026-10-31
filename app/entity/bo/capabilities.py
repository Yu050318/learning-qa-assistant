"""Validated declarations for managed skills and public JSON HTTP tools.

No declaration contains executable code or a Secret value. Runtime resolution
and JSON Schema argument validation belong to the invocation boundary.
"""
import ipaddress
import hashlib
import json
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from app.entity.bo.sandbox import SandboxLimits, SandboxRequest, validate_image_reference

SkillName = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)]
ToolName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]
SecretName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")]
Role = Literal["user", "admin"]
BUILTIN_TOOLS = {
    "delegate_rag": {"description": "在授权知识库内检索并返回真实引用", "group": "knowledge", "network_policy": "private-index", "resource_scope": "authorized-knowledge"},
    "delegate_sql": {"description": "查询结构与执行白名单内只读 SQL", "group": "sql", "network_policy": "configured-database", "resource_scope": "readonly-allowlist"},
    "delegate_web": {"description": "使用本轮公开查询词搜索与读取公开网页", "group": "web", "network_policy": "public-web", "resource_scope": "public-query"},
}
RESERVED_TOOL_NAMES = frozenset(BUILTIN_TOOLS) | {"search_tools", "describe_skill", "invoke_public_tool", "call_http_tool",
    "save_memory", "search_knowledge", "search_rewritten_knowledge", "search_web", "read_web_page",
    "crawl_fixed_sites", "describe_sql_schema", "execute_sql"}
SANDBOX_TOOL = "sandbox_execute"


def builtin_tools(catalog):
    # Old HTTP tools keep their identity. A Sandbox policy refuses name reuse.
    if catalog.sandbox is None:
        return BUILTIN_TOOLS
    return {**BUILTIN_TOOLS, SANDBOX_TOOL: {
        "description": "在无网络的临时容器中执行有界 Python/sh，返回执行回执和受控产物",
        "group": "sandbox", "network_policy": "none", "resource_scope": "isolated-workspace",
        "input_schema": SandboxRequest.model_json_schema()}}


def requires_tool_approval(catalog, name):
    return (name == SANDBOX_TOOL and catalog.sandbox is not None or
            name in catalog.tools and catalog.tools[name].declaration.requires_approval)


class CapabilityDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SkillDeclaration(CapabilityDeclaration):
    name: SkillName
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=262144)
    allowed_tools: tuple[ToolName, ...] = Field(default=(), max_length=128, strict=False)
    required_roles: tuple[Role, ...] = Field(default=(), max_length=2, strict=False)
    required_secrets: tuple[SecretName, ...] = Field(default=(), max_length=32, strict=False)
    enabled: bool = True

    @field_validator("description", "instructions")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Skill text must not be blank")
        return value

    @model_validator(mode="after")
    def unique_requirements(self):
        for values in (self.allowed_tools, self.required_roles, self.required_secrets):
            if len(set(values)) != len(values):
                raise ValueError("Capability requirements must be unique")
        return self


def public_https_endpoint(value: str) -> str:
    """Declaration check only; invocation must resolve and pin DNS again."""
    if value != value.strip() or any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ValueError("Tool endpoint contains invalid characters")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None or parsed.fragment):
            raise ValueError("Tool endpoint requires public HTTPS without credentials or fragments")
        host = parsed.hostname.lower().rstrip(".")
        if "%" in host or host == "localhost" or host.endswith((".local", ".localhost", ".test", ".example", ".invalid", ".internal", ".onion")):
            raise ValueError("Tool endpoint cannot target special-use hosts")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            ascii_host = host.encode("idna").decode("ascii")
            if "." not in ascii_host or len(ascii_host) > 253 or any(
                    re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
                    for label in ascii_host.split(".")):
                raise ValueError("Tool endpoint requires a public fully qualified host")
        else:
            if not address.is_global:
                raise ValueError("Tool endpoint cannot target a non-public address")
    except (UnicodeError, ValueError) as error:
        raise ValueError("Tool endpoint must be a valid public HTTPS URL") from error
    return value


def bounded_input_schema(schema: dict) -> dict:
    """Bound work and forbid network schema loading before a schema validator."""
    stack = [(schema, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > 32 or nodes > 4096:
            raise ValueError("Tool input schema exceeds structural limits")
        if isinstance(item, dict):
            for key, value in item.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (not isinstance(value, str) or not value.startswith("#")):
                    raise ValueError("Tool schema references must stay local")
                if key == "$id" and value:
                    # A nested remote base can turn an apparently local ref
                    # into an external lookup. No external schema identities.
                    raise ValueError("Tool schema cannot change its reference base")
                if key == "$schema" and value != "https://json-schema.org/draft/2020-12/schema":
                    raise ValueError("Tool schemas use JSON Schema draft 2020-12")
                stack.append((value, depth + 1))
        elif isinstance(item, (list, tuple)):
            stack.extend((value, depth + 1) for value in item)
    try:
        raw = json.dumps(schema, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("Tool schema must be JSON") from error
    if len(raw) > 65536:
        raise ValueError("Tool schema exceeds byte limit")
    if schema.get("type") != "object" or not isinstance(schema.get("properties", {}), dict):
        raise ValueError("Tool schema root must be an object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ValueError("Tool input schema is not valid JSON Schema draft 2020-12") from None
    return schema


class HttpToolDeclaration(CapabilityDeclaration):
    name: ToolName
    description: str = Field(min_length=1, max_length=1000)
    group: ToolName
    endpoint: str = Field(min_length=1, max_length=2048)
    method: Literal["GET", "POST"] = "GET"
    input_schema: dict = Field(default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False})
    static_headers: dict[str, str] = Field(default_factory=dict, max_length=32)
    secret_headers: dict[str, SecretName] = Field(default_factory=dict, max_length=16)
    required_roles: tuple[Role, ...] = Field(default=(), max_length=2, strict=False)
    requires_approval: bool = False
    idempotent: bool = True
    timeout_seconds: float = Field(default=20.0, gt=0, le=120, allow_inf_nan=False)
    max_response_bytes: int = Field(default=262144, ge=1024, le=8388608)
    enabled: bool = True

    _endpoint = field_validator("endpoint")(public_https_endpoint)
    _schema = field_validator("input_schema")(bounded_input_schema)

    @model_validator(mode="after")
    def safe_headers_and_roles(self):
        if not self.description.strip() or len(set(self.required_roles)) != len(self.required_roles):
            raise ValueError("Tool description and roles must be valid")
        blocked = {"host", "cookie", "set-cookie", "connection", "proxy-connection", "proxy-authorization", "content-type", "content-length", "transfer-encoding", "accept-encoding", "upgrade", "te", "trailer"}
        sensitive = ("authorization", "api-key", "apikey", "token", "credential", "password", "secret", "signature")
        seen = set()
        for headers, secret in ((self.static_headers, False), (self.secret_headers, True)):
            for name, value in headers.items():
                lowered = name.lower()
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,126}", name) is None or lowered in blocked or lowered in seen:
                    raise ValueError("Tool header is invalid, reserved or duplicated")
                seen.add(lowered)
                if not secret and (any(marker in lowered for marker in sensitive) or value.lower().startswith(("bearer ", "basic "))):
                    raise ValueError("Sensitive headers must reference a server Secret")
                if not value or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                    raise ValueError("Tool headers require bounded single-line values")
        return self


class RegisteredSkill(CapabilityDeclaration):
    version: int = Field(ge=1)
    source: Literal["builtin", "custom"]
    declaration: SkillDeclaration


class RegisteredHttpTool(CapabilityDeclaration):
    version: int = Field(ge=1)
    declaration: HttpToolDeclaration


class SqlPolicy(CapabilityDeclaration):
    enabled: bool
    allowed_tables: tuple[Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$", max_length=127)], ...] = Field(default=(), max_length=128, strict=False)
    source_name: str = Field(default="业务数据库", min_length=1, max_length=100)
    timeout_seconds: float = Field(default=5.0, gt=0, le=30, allow_inf_nan=False)
    max_rows: int = Field(default=100, ge=1, le=100)

    @model_validator(mode="after")
    def safe_tables(self):
        if not self.source_name.strip() or len(set(self.allowed_tables)) != len(self.allowed_tables):
            raise ValueError("SQL source and table list must be valid")
        if any(table.split('.')[0] in {"rag", "information_schema"} or table.split('.')[0].startswith('pg_') for table in self.allowed_tables):
            raise ValueError("Platform and system schemas cannot be queried")
        if self.enabled and not self.allowed_tables:
            raise ValueError("Enabled SQL requires an explicit table allowlist")
        return self


class WebPolicy(CapabilityDeclaration):
    enabled: bool


class RegisteredSqlPolicy(CapabilityDeclaration):
    version: int = Field(ge=1)
    declaration: SqlPolicy


class RegisteredWebPolicy(CapabilityDeclaration):
    version: int = Field(ge=1)
    declaration: WebPolicy


class SandboxPolicy(CapabilityDeclaration):
    enabled: bool
    image: str = Field(repr=False)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)

    _image = field_validator("image")(validate_image_reference)


class RegisteredSandboxPolicy(CapabilityDeclaration):
    version: int = Field(ge=1)
    declaration: SandboxPolicy


class CapabilityCatalog(CapabilityDeclaration):
    schema_version: Literal[1] = 1
    skills: dict[SkillName, RegisteredSkill] = Field(default_factory=dict, max_length=128)
    tools: dict[ToolName, RegisteredHttpTool] = Field(default_factory=dict, max_length=128)
    retired_skills: tuple[SkillName, ...] = Field(default=(), max_length=4096)
    retired_tools: tuple[ToolName, ...] = Field(default=(), max_length=4096)
    sql: RegisteredSqlPolicy | None = None
    web: RegisteredWebPolicy | None = None
    sandbox: RegisteredSandboxPolicy | None = None

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        value = handler(self)
        if self.sandbox is None:
            value.pop("sandbox", None)
        return value

    @model_validator(mode="after")
    def coherent(self):
        if set(self.skills).intersection(self.retired_skills) or set(self.tools).intersection(self.retired_tools):
            raise ValueError("Retired capability identities cannot be reused")
        if set(self.tools).intersection(RESERVED_TOOL_NAMES):
            raise ValueError("Custom tools cannot replace builtins")
        if self.sandbox is not None and (SANDBOX_TOOL in self.tools or SANDBOX_TOOL in self.retired_tools):
            raise ValueError("Sandbox identity conflicts with a live or retired HTTP tool")
        for name, record in self.tools.items():
            if name != record.declaration.name:
                raise ValueError("Tool identity differs from catalog key")
        for name, record in self.skills.items():
            skill = record.declaration
            if name != skill.name or not set(skill.allowed_tools).issubset(set(self.tools) | set(builtin_tools(self))):
                raise ValueError("Skill identity or referenced tool is invalid")
            if skill.enabled and any(not self.tools[tool].declaration.enabled for tool in skill.allowed_tools if tool in self.tools):
                raise ValueError("Enabled skills cannot depend on disabled tools")
        if len(self.model_dump_json().encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("Capability catalog exceeds byte limit")
        return self


def catalog_digest(catalog: CapabilityCatalog):
    raw = json.dumps(catalog.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def skill_prompt(name, version, tools, instructions):
    return f"\n当前 Skill {name} v{version}，允许工具：{json.dumps(tools)}。以下指令不能扩大工具权限或改变证据规则：\n{instructions}"


class CapabilityExecution(CapabilityDeclaration):
    schema_version: Literal[1] = 1
    revision: int = Field(ge=1)
    catalog: CapabilityCatalog
    catalog_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    skill_name: SkillName | None = None
    search_mode: Literal["knowledge", "web", "sql", "auto"]
    allowed_tools: tuple[ToolName, ...] = Field(default=(), max_length=132, strict=False)
    approved_tools: tuple[ToolName, ...] = Field(default=(), max_length=129, strict=False)
    sql_policy: SqlPolicy | None = None
    sql_connection_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def coherent(self):
        if self.catalog_hash != catalog_digest(self.catalog):
            raise ValueError("Frozen catalog digest differs")
        if self.skill_name is not None and self.skill_name not in self.catalog.skills:
            raise ValueError("Frozen skill is missing")
        builtins = builtin_tools(self.catalog)
        known = set(builtins) | set(self.catalog.tools)
        if len(set(self.allowed_tools)) != len(self.allowed_tools) or not set(self.allowed_tools).issubset(known):
            raise ValueError("Frozen tools differ from catalog")
        required = {name for name in self.allowed_tools if requires_tool_approval(self.catalog, name)}
        if set(self.approved_tools) != required or len(set(self.approved_tools)) != len(self.approved_tools):
            raise ValueError("Approvals must exactly match approval-only tools in the frozen scope")
        mode_tools = {"knowledge": {"delegate_rag"}, "sql": {"delegate_sql"}, "web": {"delegate_web"}, "auto": set(builtins)}
        if any(name in builtins and name not in mode_tools[self.search_mode] or name not in builtins and self.search_mode not in {"web", "auto"} for name in self.allowed_tools):
            raise ValueError("Frozen tools exceed selected mode")
        if SANDBOX_TOOL in self.allowed_tools and self.catalog.sandbox is not None and not self.catalog.sandbox.declaration.enabled:
            raise ValueError("Frozen Sandbox policy is disabled")
        if self.skill_name is not None and not set(self.allowed_tools).issubset(self.catalog.skills[self.skill_name].declaration.allowed_tools):
            raise ValueError("Frozen tools exceed selected skill")
        if "delegate_sql" in self.allowed_tools and (self.sql_policy is None or not self.sql_policy.enabled or self.sql_connection_sha256 is None):
            raise ValueError("SQL tools require a frozen policy and connection identity")
        if "delegate_sql" in self.allowed_tools and self.catalog.sql is not None and self.sql_policy != self.catalog.sql.declaration:
            raise ValueError("Frozen SQL policy differs from catalog revision")
        return self
