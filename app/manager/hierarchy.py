"""PostgreSQL hierarchy publication and bounded, owner-scoped auto merging."""
from app.manager.transactions import transaction_scope, as_transaction
from app.dao.queries import hierarchy as queries
from dataclasses import replace
from time import monotonic
from uuid import UUID


from app.dao.postgres.errors import SQLAlchemyError

from app.manager.evidence import count_tokens
from app.core.errors import AppError
from app.entity.bo.contracts import Chunk, ExcelSource, HierarchyContext, ParentContext
from app.manager.loaders.hierarchy import digest
from app.entity.do.models import DocumentRecord, HierarchyNode


def publish_tree(session, document, tree):
    if any(n["document_id"] != document.id or n["user_id"] != document.user_id for n in tree.nodes):
        raise AppError("HIERARCHY_SCOPE_MISMATCH", "层级节点不属于目标文档", 503)
    queries.remove_document_nodes(document, session)
    # Parent-before-child ordering also works with immediate PostgreSQL FK checks.
    for level in (1, 2, 3):
        rows = [n for n in tree.nodes if n["level"] == level]
        for offset in range(0, len(rows), 200):
            queries.insert_node_batch(offset, rows, session)
    metadata = dict(document.parser_metadata or {})
    metadata["hierarchy"] = tree.manifest(document.content_hash, metadata.get("generation", 0))
    document.parser_metadata = metadata


class HierarchyService:
    def __init__(self, settings, database, *, legacy_expander=None):
        self.settings = settings
        self.database = database
        self.legacy_expander = legacy_expander

    def expand(self, user_id, document_ids, hits, *, deadline=None, token_budget=None):
        if not hits:
            return hits
        allowed = set(document_ids)
        if any(h.chunk.user_id != str(user_id) or UUID(h.chunk.document_id) not in allowed for h in hits):
            raise AppError("HIERARCHY_SCOPE_MISMATCH", "层级检索超出获准文档范围", 503)
        try:
            with transaction_scope(self.database) as session:
                def remaining_time():
                    remaining = self.settings.database_timeout if deadline is None else min(self.settings.database_timeout, deadline - monotonic())
                    if remaining <= 0:
                        raise AppError("HIERARCHY_TIMEOUT", "层级检索时间预算耗尽", 504)
                    return remaining

                documents = queries.documents_for_hits(session, user_id, {UUID(h.chunk.document_id) for h in hits}, remaining_time())
                docs = {str(d.id): d for d in documents}
                if set(docs) != {h.chunk.document_id for h in hits}:
                    raise AppError("HIERARCHY_SCOPE_MISMATCH", "层级文档不可访问", 503)
                indexed, legacy, stale = [], [], []
                for hit in hits:
                    doc = docs[hit.chunk.document_id]
                    manifest = (doc.parser_metadata or {}).get("hierarchy")
                    if doc.status != "ready" or doc.embedding_model != self.settings.embedding_model:
                        stale.append(hit)
                    elif manifest is None:
                        legacy.append(hit)
                    elif (not isinstance(manifest, dict) or manifest.get("source_hash") != doc.content_hash or manifest.get("generation") != (doc.parser_metadata or {}).get("generation", 0)
                          or manifest.get("leaf_count") != doc.chunk_count):
                        stale.append(hit)
                    else:
                        indexed.append(hit)

                def nodes(node_ids, limit, *, by_parent=False):
                    rows = queries.scoped_nodes(session, user_id, [d.id for d in documents], node_ids,
                        limit, remaining_time(), by_parent=by_parent)
                    if len(rows) > limit or any(n.user_id != user_id or str(n.document_id) not in docs or n.content_hash != digest(n.text) for n in rows):
                        raise AppError("HIERARCHY_SCOPE_MISMATCH", "层级节点校验失败", 503)
                    for n in rows:
                        manifest = (docs[str(n.document_id)].parser_metadata or {}).get("hierarchy")
                        if n.fingerprint != manifest.get("fingerprint"):
                            raise AppError("HIERARCHY_INVALID", "层级版本与文档不符", 503)
                    return rows

                leaf_nodes = nodes([UUID(h.chunk.chunk_id) for h in indexed], len(indexed))
                parent_ids = {n.parent_id for n in leaf_nodes}
                parents = nodes(parent_ids, len(parent_ids))
                root_ids = {n.parent_id for n in parents}
                roots = nodes(root_ids, len(root_ids))
                siblings = nodes(root_ids, len(root_ids) * 8, by_parent=True)
                descendants = nodes([n.node_id for n in siblings], len(siblings) * 8, by_parent=True)
                enriched = self._merge(indexed, leaf_nodes, parents, roots, siblings, descendants, token_budget)
                result = {h.chunk.chunk_id: h for h in enriched}
                result.update({h.chunk.chunk_id: replace(h, parent_context=None, context_warning="HIERARCHY_STALE",
                                                       hierarchy_trace={"chunk_id": h.chunk.chunk_id, "status": "stale", "selected_level": 3}) for h in stale})
            # Legacy vector reads do not hold a SQL transaction or connection.
            for did in {h.chunk.document_id for h in legacy}:
                selected = [h for h in legacy if h.chunk.document_id == did]
                if deadline is not None and monotonic() >= deadline:
                    raise AppError("HIERARCHY_TIMEOUT", "层级检索时间预算耗尽", 504)
                batch = self.legacy_expander(user_id, [UUID(did)], selected, deadline=deadline) if self.legacy_expander else selected
                result.update({h.chunk.chunk_id: replace(h, hierarchy_trace={"chunk_id": h.chunk.chunk_id, "status": "legacy", "selected_level": None}) for h in batch})
            return [result[h.chunk.chunk_id] for h in hits]
        except SQLAlchemyError as error:
            code = "HIERARCHY_TIMEOUT" if getattr(getattr(error, "orig", None), "sqlstate", None) == "57014" else "HIERARCHY_UNAVAILABLE"
            raise AppError(code, "层级检索暂不可用，保留叶子证据", 503) from None

    def _merge(self, hits, leaf_nodes, parents, roots, siblings, descendants, token_budget):
        def invalid():
            raise AppError("HIERARCHY_SCOPE_MISMATCH", "层级结构或来源边界不一致", 503)

        def decode(node):
            try:
                metadata = dict(node.node_metadata)
                source = metadata.pop("excel_source", None)
                return Chunk(str(node.node_id), str(node.user_id), str(node.document_id), node.leaf_index,
                             node.text, **metadata, excel_source=ExcelSource(**source) if source else None)
            except (TypeError, ValueError):
                invalid()

        by_id = {n.node_id: n for n in [*roots, *siblings, *descendants]}
        child_map = {}
        for node in [*siblings, *descendants]:
            parent = by_id.get(node.parent_id)
            if (parent is None or parent.level != node.level - 1 or node.level not in {2, 3}
                    or (node.user_id, node.document_id) != (parent.user_id, parent.document_id)
                    or not node.text or len(node.text) > 48000 or node.text not in parent.text):
                invalid()
            child_map.setdefault(node.parent_id, []).append(node)
        if any(r.level != 1 or r.parent_id is not None for r in roots) or any(len(c) > 8 for c in child_map.values()):
            invalid()
        # Do not accept a selected node if the descendant query returned a different row.
        if any(by_id.get(n.node_id) is None or by_id[n.node_id].text != n.text or by_id[n.node_id].parent_id != n.parent_id for n in [*parents, *leaf_nodes]):
            invalid()
        chunks = {n.node_id: decode(n) for n in descendants}
        for parent in siblings:
            children = child_map.get(parent.node_id, [])
            positions = [chunks[n.node_id].chunk_index for n in children]
            if not children or len(positions) != len(set(positions)) or any(type(i) is not int or i < 0 for i in positions):
                invalid()
        for ancestor in [*roots, *siblings]:
            children = child_map.get(ancestor.node_id, [])
            leaf_count = len(children) if ancestor.level == 2 else sum(len(child_map.get(p.node_id, [])) for p in children)
            if (len(children) != ancestor.node_metadata.get("child_count")
                    or leaf_count != ancestor.node_metadata.get("descendant_leaf_count")):
                invalid()
        matched = set()
        for hit in hits:
            identity = UUID(hit.chunk.chunk_id)
            if identity in chunks:
                if chunks[identity] != hit.chunk:
                    invalid()
                matched.add(identity)
        threshold = self.settings.hierarchy_merge_threshold
        eligible = set()
        for parent in siblings:
            children = child_map[parent.node_id]
            if sum(c.node_id in matched for c in children) / len(children) >= threshold:
                eligible.add(parent.node_id)
        for root in roots:
            children = child_map.get(root.node_id, [])
            if children and sum(c.node_id in eligible for c in children) / len(children) >= threshold:
                eligible.add(root.node_id)

        def context(node):
            leaves = child_map[node.node_id] if node.level == 2 else [leaf for p in child_map[node.node_id] for leaf in child_map[p.node_id]]
            children = tuple(sorted((chunks[n.node_id] for n in leaves), key=lambda c: c.chunk_index))
            try:
                return HierarchyContext(str(node.node_id), node.level, node.text, children)
            except ValueError:
                invalid()

        # Validate ALL fetched contexts, including currently unselected siblings.
        for n in [*roots, *siblings]:
            leaves = child_map.get(n.node_id, [])
            if n.level == 1:
                leaves = [c for p in leaves for c in child_map.get(p.node_id, [])]
            if len(leaves) >= 2:
                context(n)
        budget = max(0, token_budget) if token_budget is not None else 4000
        charged = set()
        result = []
        for hit in hits:
            leaf = by_id.get(UUID(hit.chunk.chunk_id))
            chosen, reason = None, "coverage"
            if leaf:
                parent = by_id.get(leaf.parent_id)
                root = by_id.get(parent.parent_id) if parent else None
                for ancestor in (root, parent):
                    if ancestor is None or ancestor.node_id not in eligible:
                        continue
                    children = child_map[ancestor.node_id]
                    leaf_count = len(children) if ancestor.level == 2 else sum(len(child_map[p.node_id]) for p in children)
                    if leaf_count < 2:
                        continue
                    tokens = count_tokens(ancestor.text)
                    if ancestor.node_id not in charged and tokens > budget:
                        reason = "budget"
                        continue
                    chosen = context(ancestor)
                    if ancestor.node_id not in charged:
                        budget -= tokens
                        charged.add(ancestor.node_id)
                    reason = "merged"
                    break
            else:
                reason = "stale"
            selected = by_id[UUID(chosen.node_id)] if chosen else by_id.get(leaf.parent_id) if leaf else None
            children = child_map.get(selected.node_id, []) if selected else []
            matches = sum(c.node_id in (matched if selected.level == 2 else eligible) for c in children) if selected else 0
            result.append(replace(hit, parent_context=chosen,
                                  context_warning="HIERARCHY_STALE" if reason == "stale" else "PARENT_CONTEXT_BUDGET_EXCEEDED" if reason == "budget" else None,
                                  hierarchy_trace={"chunk_id": hit.chunk.chunk_id, "document_id": hit.chunk.document_id,
                                                   "status": reason, "selected_level": chosen.level if chosen else 3,
                                                   "node_id": chosen.node_id if chosen else hit.chunk.chunk_id,
                                                   "matched_children": matches, "total_children": len(children)}))
        return result
