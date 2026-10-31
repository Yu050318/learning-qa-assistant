"""One transactional, versioned catalog shared by API and independent workers."""
from app.manager.transactions import transaction_scope, as_transaction
from app.dao.queries import capabilities as queries
import json
import os
import re
import hashlib

from pydantic import ValidationError


from app.core.errors import AppError
from app.entity.bo.capabilities import BUILTIN_TOOLS, RESERVED_TOOL_NAMES, SANDBOX_TOOL, CapabilityCatalog, CapabilityExecution, HttpToolDeclaration, SkillDeclaration, SqlPolicy, WebPolicy, SandboxPolicy, builtin_tools, requires_tool_approval, catalog_digest
from app.entity.do.models import CapabilityConfiguration, CapabilityConfigurationVersion, User, utcnow


class CapabilityService:
    def __init__(self, settings, database):
        self.settings, self.database = settings, database

    def require(self):
        if not self.settings.capability_center_enabled:
            raise AppError("CAPABILITIES_DISABLED", "能力中心尚未启用", 503)

    @staticmethod
    def user(session, uid, *, admin=False):
        user = queries.get_user_for_validation(session, uid)
        if user is None or not user.is_active or admin and user.role != "admin":
            raise AppError("FORBIDDEN", "需要有效的管理员权限" if admin else "账号不可用", 403)
        return user

    @staticmethod
    def read(session, *, lock=False, shared=False):
        row = queries.get_configuration(lock, session, shared)
        if row is None:
            raise AppError("CAPABILITY_MIGRATION_REQUIRED", "请先执行数据库迁移初始化能力目录", 503)
        try:
            catalog = CapabilityCatalog.model_validate_json(json.dumps(row.configuration, ensure_ascii=False, allow_nan=False))
        except (ValidationError, ValueError, TypeError):
            raise AppError("INVALID_CAPABILITY_CATALOG", "已保存的能力目录无法通过校验", 503) from None
        return row, catalog

    def snapshot(self, session=None):
        self.require()
        if session is None:
            with transaction_scope(self.database) as current:
                return self.snapshot(current)
        row, catalog = self.read(session)
        return row.revision, catalog

    def control_plane(self, uid):
        self.require()
        with transaction_scope(self.database) as session:
            self.user(session, uid, admin=True)
            row, catalog = self.read(session)
            return {"revision": row.revision, "catalog": catalog.model_dump(mode="json"),
                    "builtin_tools": builtin_tools(catalog), "secret_names": sorted(self.settings.capability_secret_names),
                    "runtime": {"sql": self.sql_policy(catalog).model_dump(mode="json"), "web": {"enabled": catalog.web is None or catalog.web.declaration.enabled},
                        "sql_connection_configured": bool(self.settings.sql_database_url.get_secret_value()), "web_search_configured": self.settings.web_search_enabled}}

    @staticmethod
    def persist(session, row, uid, catalog, change):
        row.revision += 1
        row.configuration = catalog.model_dump(mode="json")
        row.updated_at = utcnow()
        as_transaction(session).save(CapabilityConfigurationVersion(revision=row.revision, configuration=row.configuration,
            created_by=uid, change=change))
        session.commit()
        return {"revision": row.revision, "catalog": row.configuration}

    def sql_policy(self, catalog):
        if catalog.sql is not None:
            return catalog.sql.declaration
        return SqlPolicy(enabled=bool(self.settings.sql_database_url.get_secret_value() and self.settings.sql_allowed_tables),
            allowed_tables=self.settings.sql_allowed_tables, source_name=self.settings.sql_source_name,
            timeout_seconds=float(self.settings.sql_timeout), max_rows=self.settings.sql_max_rows)

    @staticmethod
    def project_sql(settings, policy):
        return settings.model_copy(update={"sql_allowed_tables": list(policy.allowed_tables) if policy.enabled else [],
            "sql_source_name": policy.source_name, "sql_timeout": policy.timeout_seconds, "sql_max_rows": policy.max_rows})

    def sql_connection_identity(self):
        try:
            url = queries.connection_identity(self.settings.sql_database_url.get_secret_value())
            return hashlib.sha256(url.render_as_string(hide_password=False).encode()).hexdigest()
        except Exception:
            raise AppError("SQL_CONFIGURATION_ERROR", "请配置有效的独立只读连接", 503) from None

    def update_runtime(self, uid, component, policy, expected_revision):
        self.require()
        policy_types = {"sql": SqlPolicy, "web": WebPolicy, "sandbox": SandboxPolicy}
        if component not in policy_types or not isinstance(policy, policy_types[component]):
            raise AppError("INVALID_CAPABILITY", "运行配置类型不一致", 422)
        with transaction_scope(self.database) as session:
            self.user(session, uid, admin=True)
            row, catalog = self.read(session, lock=True)
            if row.revision != expected_revision:
                raise AppError("CAPABILITY_REVISION_CONFLICT", "目录已更新，请刷新后重试", 409)
            if component == "sandbox" and (SANDBOX_TOOL in catalog.tools or SANDBOX_TOOL in catalog.retired_tools):
                raise AppError("SANDBOX_IDENTITY_CONFLICT", "已有或已退休的同名 HTTP 工具，不能重新解释其身份", 409)
            if component == "sql" and policy.enabled:
                from app.manager.sql import ReadOnlySQL
                sql = ReadOnlySQL(self.project_sql(self.settings, policy))
                try:
                    sql.schema(policy.timeout_seconds)  # Reuse actual read-only role/table validation.
                finally:
                    sql.close()
            raw = catalog.model_dump(mode="json")
            previous = raw.get(component)
            raw[component] = {"version": previous["version"] + 1 if previous else 1, "declaration": policy.model_dump(mode="json")}
            updated = CapabilityCatalog.model_validate_json(json.dumps(raw))
            return self.persist(session, row, uid, updated, {"operation": "update", "kind": "runtime", "name": component})

    def change(self, uid, kind, name, *, expected_revision, declaration=None, delete=False, create=False):
        self.require()
        if kind not in {"skills", "tools"}:
            raise AppError("INVALID_CAPABILITY_KIND", "能力类型无效", 422)
        expected_type = SkillDeclaration if kind == "skills" else HttpToolDeclaration
        if not delete and (not isinstance(declaration, expected_type) or declaration.name != name):
            raise AppError("INVALID_CAPABILITY", "注册内容和能力标识不一致", 422)
        if kind == "tools" and name in RESERVED_TOOL_NAMES:
            raise AppError("BUILTIN_TOOL_PROTECTED", "内置工具不能覆盖或删除", 409)
        with transaction_scope(self.database) as session:
            self.user(session, uid, admin=True)
            row, catalog = self.read(session, lock=True)
            if row.revision != expected_revision:
                raise AppError("CAPABILITY_REVISION_CONFLICT", "目录已更新，请刷新后重试", 409)
            if kind == "tools" and name == SANDBOX_TOOL and catalog.sandbox is not None:
                raise AppError("BUILTIN_TOOL_PROTECTED", "Sandbox 内置工具不能覆盖或删除", 409)
            raw = catalog.model_dump(mode="json")
            current = raw[kind].get(name)
            if not delete and create and current is not None:
                raise AppError("CAPABILITY_EXISTS", "能力标识已存在", 409)
            if not delete and not create and current is None:
                raise AppError("NOT_FOUND", "能力不存在", 404)
            if delete:
                if current is None:
                    raise AppError("NOT_FOUND", "能力不存在", 404)
                if kind == "tools" and any(name in item.declaration.allowed_tools for item in catalog.skills.values()):
                    raise AppError("CAPABILITY_IN_USE", "仍有 Skill 引用该工具，请先移除引用", 409)
                del raw[kind][name]
                raw["retired_" + kind].append(name)
                operation = "delete"
            else:
                if name in raw["retired_" + kind]:
                    raise AppError("CAPABILITY_RETIRED", "已删除的能力标识不能重用", 409)
                references = set(declaration.required_secrets) if kind == "skills" else set(declaration.secret_headers.values())
                if not references.issubset(self.settings.capability_secret_names):
                    raise AppError("CAPABILITY_SECRET_NOT_ALLOWED", "只能引用服务端明确允许的 Secret 名称", 422)
                raw[kind][name] = {"version": current["version"] + 1 if current else 1,
                                   "declaration": declaration.model_dump(mode="json")}
                if kind == "skills":
                    raw[kind][name]["source"] = current["source"] if current else "custom"
                operation = "update" if current else "create"
            try:
                updated = CapabilityCatalog.model_validate_json(json.dumps(raw, ensure_ascii=False, allow_nan=False))
            except ValidationError:
                raise AppError("INVALID_CAPABILITY_DEPENDENCIES", "目录依赖无效：检查工具引用、启用状态和目录大小", 409) from None
            return self.persist(session, row, uid, updated, {"operation": operation, "kind": kind, "name": name})

    def history(self, uid, offset=0, limit=20):
        self.require()
        with transaction_scope(self.database) as session:
            self.user(session, uid, admin=True)
            rows = queries.configuration_history(limit, offset, session)
            return {"items": [{"revision": row.revision, "created_by": row.created_by,
                "created_at": row.created_at, "change": row.change, "catalog": row.configuration} for row in rows],
                "offset": offset, "limit": limit}

    def secret_available(self, name):
        return name in self.settings.capability_secret_names and bool(os.environ.get(name))

    def tool_reason(self, catalog, role, name):
        if name == SANDBOX_TOOL and catalog.sandbox is not None:
            return ("disabled" if not self.settings.sandbox_enabled or not catalog.sandbox.declaration.enabled
                else "approval_not_available" if not self.settings.tool_approvals_enabled or not self.settings.chat_runs_enabled
                else "permission_required" if role != "admin" else None)
        if name in BUILTIN_TOOLS:
            if name == "delegate_sql":
                if not self.sql_policy(catalog).enabled:
                    return "disabled" if catalog.sql else "not_configured"
                if not self.settings.sql_database_url.get_secret_value():
                    return "not_configured"
            if name == "delegate_web" and catalog.web is not None and not catalog.web.declaration.enabled:
                return "disabled"
            return None
        item = catalog.tools.get(name)
        if item is None:
            return "deleted"
        tool = item.declaration
        return ("disabled" if not tool.enabled else "permission_required" if tool.required_roles and role not in tool.required_roles
            else "not_configured" if any(not self.secret_available(secret) for secret in tool.secret_headers.values())
            else ("approval_not_available" if not self.settings.tool_approvals_enabled else "permission_required" if role != "admin" else None) if tool.requires_approval else None)

    def skill_reason(self, catalog, role, name):
        item = catalog.skills.get(name)
        if item is None:
            return "deleted"
        skill = item.declaration
        return ("disabled" if not skill.enabled else "permission_required" if skill.required_roles and role not in skill.required_roles
            else "not_configured" if any(not self.secret_available(secret) for secret in skill.required_secrets)
            else next((reason for tool in skill.allowed_tools if (reason := self.tool_reason(catalog, role, tool))), None))

    def freeze(self, uid, *, skill_name=None, search_mode="auto", public_query=None, approved_tools=(), approval_catalog_revision=None, session=None):
        self.require()
        if session is None:
            with transaction_scope(self.database) as current:
                return self.freeze(uid, skill_name=skill_name, search_mode=search_mode, public_query=public_query,
                    approved_tools=approved_tools, approval_catalog_revision=approval_catalog_revision, session=current)
        role = self.user(session, uid).role
        row, catalog = self.read(session)
        if not isinstance(approved_tools, (list, tuple)) or any(not isinstance(name, str) for name in approved_tools) or len(set(approved_tools)) != len(approved_tools):
            raise AppError("INVALID_TOOL_APPROVAL", "批准工具须为不重复的名称", 422)
        if bool(approved_tools) != (approval_catalog_revision is not None):
            raise AppError("INVALID_TOOL_APPROVAL", "批准工具必须携带所确认的目录修订", 422)
        if approved_tools:
            if not self.settings.chat_runs_enabled:
                raise AppError("CAPABILITY_DURABLE_RUN_REQUIRED", "工具批准必须使用持久 Run", 503)
            if not self.settings.tool_approvals_enabled:
                raise AppError("TOOL_APPROVALS_DISABLED", "Run 工具审批尚未启用", 503)
            if role != "admin":
                raise AppError("FORBIDDEN", "只有平台管理员可为本次 Run 批准高风险工具", 403)
            if type(approval_catalog_revision) is not int or approval_catalog_revision != row.revision:
                raise AppError("CAPABILITY_REVISION_CONFLICT", "能力目录已变化，请核对工具后重新确认", 409)
            if any(not requires_tool_approval(catalog, name) for name in approved_tools):
                raise AppError("INVALID_TOOL_APPROVAL", "只能批准当前目录中声明需要审批的工具", 422)
        if skill_name is not None and self.skill_reason(catalog, role, skill_name):
            raise AppError("SKILL_UNAVAILABLE", "该 Skill 不存在、尚未配置或当前账号无权使用", 403)
        builtins = builtin_tools(catalog)
        modes = {"knowledge": {"delegate_rag"}, "sql": {"delegate_sql"}, "web": {"delegate_web"},
                 "auto": set(builtins)}
        candidates = (catalog.skills[skill_name].declaration.allowed_tools if skill_name is not None
                      else (*builtins, *catalog.tools))
        allowed = []
        for name in candidates:
            if name in builtins and name not in modes[search_mode] or name not in builtins and search_mode not in {"web", "auto"}:
                if skill_name is not None:
                    raise AppError("SKILL_MODE_CONFLICT", "当前模式与 Skill 的工具范围不兼容，请切换为自动模式", 422)
                continue
            if self.tool_reason(catalog, role, name):
                continue
            if requires_tool_approval(catalog, name) and name not in approved_tools:
                if skill_name is not None:
                    raise AppError("TOOL_APPROVAL_REQUIRED", "此 Skill 需要为本次 Run 明确批准工具", 403)
                continue
            if name in catalog.tools and not public_query:
                if skill_name is not None:
                    raise AppError("HTTP_TOOL_PUBLIC_QUERY_REQUIRED", "请提供本轮允许发送给公共工具的查询内容", 422)
                continue
            allowed.append(name)
        if not set(approved_tools).issubset(allowed):
            raise AppError("INVALID_TOOL_APPROVAL", "批准工具超出了本次模式、Skill 或公开输入范围", 422)
        return CapabilityExecution(revision=row.revision, catalog=catalog, catalog_hash=catalog_digest(catalog),
            skill_name=skill_name, search_mode=search_mode, allowed_tools=tuple(allowed),
            approved_tools=tuple(sorted(approved_tools)),
            sql_policy=self.sql_policy(catalog) if "delegate_sql" in allowed else None,
            sql_connection_sha256=self.sql_connection_identity() if "delegate_sql" in allowed else None)

    @staticmethod
    def activation(question, skill_name=None, *, allow_empty=False):
        text = question.strip()
        match = re.match(r"^/([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s+|$)", text)
        if match is None or match[1] == "remember":
            return question, skill_name
        if skill_name is not None and skill_name != match[1]:
            raise AppError("SKILL_ACTIVATION_CONFLICT", "斜杠命令与所选 Skill 不一致", 422)
        question = text[match.end():].strip()
        if not question and not allow_empty:
            raise AppError("SKILL_QUERY_REQUIRED", "请在 Skill 命令后输入问题", 422)
        return question, match[1]

    def describe_skill(self, uid, name, *, frozen=None):
        self.require()
        with transaction_scope(self.database) as session:
            role = self.user(session, uid).role
            _, current = self.read(session)
            catalog = current
            if frozen is not None:
                execution = self.validate_execution(uid, frozen, session=session)
                if execution.skill_name is not None and execution.skill_name != name:
                    raise AppError("TOOL_NOT_ALLOWED", "当前 Skill 不能加载其他 Skill 指令", 403)
                catalog = execution.catalog
            if self.skill_reason(catalog, role, name) or self.skill_reason(current, role, name):
                raise AppError("SKILL_UNAVAILABLE", "该 Skill 不存在、未启用或当前账号无权查看", 403)
            item = catalog.skills[name]
            skill = item.declaration
            return {"status": "ok", "name": name, "version": item.version, "source": item.source,
                "description": skill.description, "instructions": skill.instructions, "tool_names": list(skill.allowed_tools)}

    def validate_execution(self, uid, frozen, *, tool_name=None, session=None, lock=False):
        if session is None:
            with transaction_scope(self.database) as current:
                return self.validate_execution(uid, frozen, tool_name=tool_name, session=current, lock=lock)
        try:
            self.require()
            # Never substitute the current catalog for an older run snapshot.
            raw = frozen.model_dump_json() if isinstance(frozen, CapabilityExecution) else json.dumps(frozen)
            execution = CapabilityExecution.model_validate_json(raw)
            role = self.user(session, uid).role
            if execution.approved_tools and (not self.settings.tool_approvals_enabled or role != "admin"):
                raise ValueError("Approval is disabled or administrator role revoked")
            _, current = self.read(session, lock=lock, shared=True)
            history = as_transaction(session).load(CapabilityConfigurationVersion, execution.revision)
            if history is None or catalog_digest(CapabilityCatalog.model_validate_json(json.dumps(history.configuration))) != execution.catalog_hash:
                raise ValueError("Frozen catalog does not match retained revision")
            if execution.skill_name is not None and any(self.skill_reason(catalog, role, execution.skill_name) for catalog in (execution.catalog, current)):
                raise ValueError("Skill authorization changed")
            if "delegate_sql" in execution.allowed_tools:
                policy = self.sql_policy(current)
                if (not policy.enabled or execution.sql_connection_sha256 != self.sql_connection_identity()
                        or not set(execution.sql_policy.allowed_tables).issubset(policy.allowed_tables)):
                    raise ValueError("SQL authorization or connection changed")
            if any(self.tool_reason(current, role, name) for name in execution.allowed_tools):
                raise ValueError("Frozen tool authorization changed")
            if any(requires_tool_approval(current, name) and name not in execution.approved_tools for name in execution.allowed_tools):
                raise ValueError("Tool now requires approval not present in the frozen Run")
            if tool_name is not None and (tool_name not in execution.allowed_tools or any(
                    self.tool_reason(catalog, role, tool_name) for catalog in (execution.catalog, current))):
                raise ValueError("Tool authorization changed")
            return execution
        except (AppError, ValueError, TypeError):
            raise AppError("CAPABILITY_SCOPE_CHANGED", "能力已停用、授权已改变或冻结版本不可核验", 409) from None

    @staticmethod
    def validate_approval(execution, approval, **identity):
        from app.entity.bo.tool_approvals import validate_run_approval
        try:
            return validate_run_approval(approval, execution.approved_tools, catalog_hash=execution.catalog_hash, **identity)
        except (ValueError, TypeError):
            raise AppError("CAPABILITY_SCOPE_CHANGED", "工具批准与当前 Run 或能力版本不一致", 409) from None

    def public_catalog(self, uid):
        self.require()
        with transaction_scope(self.database) as session:
            role = self.user(session, uid).role
            row, catalog = self.read(session)
            tools = []
            for name, metadata in builtin_tools(catalog).items():
                reason = self.tool_reason(catalog, role, name)
                sandbox = name == SANDBOX_TOOL
                policy = catalog.sandbox if sandbox else catalog.sql if name == "delegate_sql" else catalog.web if name == "delegate_web" else None
                # Explicit page reading remains available when search is disabled.
                tools.append({"name": name, "version": policy.version if policy else 1, **metadata, "available": reason is None,
                    "availability_reason": reason, "required_roles": ["admin"] if sandbox else [],
                    "requires_approval": sandbox, "idempotent": not sandbox, "exposure": "resident",
                    **({"sandbox_limits": policy.declaration.limits.model_dump(mode="json")} if sandbox else {})})
            for name, item in catalog.tools.items():
                tool = item.declaration
                reason = self.tool_reason(catalog, role, name)
                tools.append({"name": name, "version": item.version, "description": tool.description,
                    "group": tool.group, "available": reason is None, "availability_reason": reason,
                    "required_roles": list(tool.required_roles), "requires_approval": tool.requires_approval,
                    "idempotent": tool.idempotent, "network_policy": "public-https", "resource_scope": "public-web", "exposure": "deferred"})
            availability = {item["name"]: item for item in tools}
            skills = []
            for name, item in catalog.skills.items():
                skill = item.declaration
                reason = self.skill_reason(catalog, role, name)
                skills.append({"name": name, "version": item.version, "description": skill.description,
                    "activation": "/" + name, "required_roles": list(skill.required_roles),
                    "tool_names": list(skill.allowed_tools), "available": reason is None, "availability_reason": reason,
                    "approval_tools": [tool for tool in skill.allowed_tools if availability[tool]["requires_approval"]]})
            digest = catalog_digest(catalog)
            return {"schema_version": 1, "revision": row.revision, "catalog_hash": digest, "skills": skills, "tools": tools}
