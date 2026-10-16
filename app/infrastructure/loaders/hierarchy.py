"""Three-level source trees. Only leaf Chunk objects enter the vector index."""
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from itertools import groupby
from uuid import UUID, uuid5

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.application.evidence import count_tokens
from app.core.errors import AppError
from app.domain.contracts import Chunk, ExcelSource, ParentContext


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class HierarchyTree:
    leaves: list[Chunk]
    nodes: list[dict]
    fingerprint: str
    mode: str

    def manifest(self, source_hash, generation):
        return {"version": 1, "fingerprint": self.fingerprint, "mode": self.mode,
                "source_hash": source_hash, "generation": generation,
                "leaf_count": len(self.leaves), "node_count": len(self.nodes),
                "root_count": sum(n["level"] == 1 for n in self.nodes),
                "parent_count": sum(n["level"] == 2 for n in self.nodes)}


class HierarchyBuilder:
    def __init__(self, settings):
        self.settings = settings
        self.fingerprint = digest(json.dumps({"version": 1, "size": settings.chunk_size,
                                             "overlap": settings.chunk_overlap, "factors": [4, 2, 1],
                                             "fanout": 8, "encoding": "cl100k_base"}, sort_keys=True))

    def _split(self, text, factor):
        # A character bound also covers low-token-density and unusual Unicode input.
        splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base", chunk_size=self.settings.chunk_size * factor,
            chunk_overlap=self.settings.chunk_overlap if factor == 1 else 0,
            separators=["\n\n", "\n", "。", "！", "？", ". ", " ", ""], disallowed_special=())
        parts = splitter.split_text(text)
        if any(len(part) > 48000 for part in parts):
            raise AppError("HIERARCHY_TEXT_TOO_LARGE", "层级切片文本超出限制", 413)
        return parts

    def build(self, documents, user_id, document_id, source_name):
        """Split each normalized page/section recursively; never join separate units."""
        groups = []
        leaves = []
        for unit in documents:
            text = re.sub(r"\n{3,}", "\n\n", unit.page_content.replace("\r\n", "\n").replace("\x00", "")).strip()
            if not text:
                continue
            metadata = unit.metadata
            if metadata.get("excel_source"):
                # Excel parser already produced bounded rows/fragments with exact anchors.
                parts = [(text, [(text, [text])])]
            else:
                parts = [(root, [(parent, self._split(parent, 1)) for parent in self._split(root, 2)])
                         for root in self._split(text, 4)]
            for root_text, parents in parts:
                parent_groups = []
                for parent_text, texts in parents:
                    children = []
                    for content in texts:
                        index = len(leaves)
                        chunk = Chunk(str(uuid5(document_id, f"hierarchy:{self.fingerprint}:{index}:{digest(content)}")),
                                      str(user_id), str(document_id), index, content, source_name,
                                      metadata.get("page_number"), metadata.get("section"),
                                      ExcelSource(**metadata["excel_source"]) if metadata.get("excel_source") else None)
                        leaves.append(chunk)
                        if len(leaves) > self.settings.max_document_chunks:
                            raise AppError("TOO_MANY_CHUNKS", "文档切片数量超出限制", 413)
                        children.append(chunk)
                    parent_groups.extend((parent_text, children[i:i + 8]) for i in range(0, len(children), 8))
                # Bounds remain valid even if recursive separator choices yield many children.
                groups.extend((root_text, parent_groups[i:i + 8]) for i in range(0, len(parent_groups), 8))
        # Adjacent Excel rows can share context within their sheet. Text roots retain their unit boundaries.
        if leaves and all(leaf.excel_source for leaf in leaves):
            return self.from_leaves(leaves, mode="recursive-rows")
        return self._tree(leaves, groups, "recursive")

    def from_leaves(self, leaves, *, mode="backfilled-leaves"):
        """Explicit upgrade: preserve every original leaf identity, text and anchor."""
        if any(c.source_name.lower().endswith((".xls", ".xlsx")) and not c.excel_source and not c.section for c in leaves):
            raise AppError("HIERARCHY_BOUNDARY_UNKNOWN", "旧 Excel 缺少 Sheet 来源，需显式重新上传", 409)
        parents = []
        roots = []
        for _, same_boundary in groupby(leaves, key=ParentContext.boundary):
            current = []
            for leaf in same_boundary:
                candidate = current + [leaf]
                if current and (len(candidate) > 8 or count_tokens("\n\n".join(c.content for c in candidate)) > self.settings.chunk_size * 2
                                or len("\n\n".join(c.content for c in candidate)) > 24000):
                    parents.append(current)
                    current = []
                if len(leaf.content) > 48000:
                    raise AppError("HIERARCHY_TEXT_TOO_LARGE", "旧切片超出层级上下文长度限制", 413)
                current.append(leaf)
            if current:
                parents.append(current)
            current_root = []
            for parent in parents:
                candidate = current_root + [parent]
                text = "\n\n".join(c.content for p in candidate for c in p)
                if current_root and (len(candidate) > 8 or count_tokens(text) > self.settings.chunk_size * 4 or len(text) > 48000):
                    roots.append(("\n\n".join(c.content for p in current_root for c in p),
                                  [("\n\n".join(c.content for c in p), p) for p in current_root]))
                    current_root = []
                current_root.append(parent)
            if current_root:
                roots.append(("\n\n".join(c.content for p in current_root for c in p),
                              [("\n\n".join(c.content for c in p), p) for p in current_root]))
            parents = []
        return self._tree(leaves, roots, mode)

    def _tree(self, leaves, groups, mode):
        if not leaves or len(leaves) > self.settings.max_document_chunks:
            raise AppError("TOO_MANY_CHUNKS", "文档切片数量为空或超出限制", 413)
        first = leaves[0]
        if ([c.chunk_index for c in leaves] != list(range(len(leaves)))
                or len({c.chunk_id for c in leaves}) != len(leaves)
                or any((c.user_id, c.document_id, c.source_name) != (first.user_id, first.document_id, first.source_name) for c in leaves)):
            raise AppError("HIERARCHY_SCOPE_MISMATCH", "层级来源或切片序号不一致", 503)
        nodes = []

        def node(level, text, children, parent_id=None, leaf=None):
            if not text or len(text) > 48000:
                raise AppError("HIERARCHY_TEXT_TOO_LARGE", "层级文本为空或超出限制", 413)
            identity = leaf.chunk_id if leaf else str(uuid5(UUID(first.document_id), f"hierarchy:{self.fingerprint}:{mode}:{level}:{','.join(c.chunk_id for c in children)}"))
            metadata = asdict(leaf or children[0])
            for key in ("chunk_id", "user_id", "document_id", "chunk_index", "content"):
                metadata.pop(key)
            nodes.append(dict(node_id=UUID(identity), document_id=UUID(first.document_id), user_id=UUID(first.user_id),
                              parent_id=parent_id, level=level, leaf_index=leaf.chunk_index if leaf else None,
                              text=text, content_hash=digest(text), fingerprint=self.fingerprint, node_metadata=metadata))
            return UUID(identity)

        for root_text, parents in groups:
            children = [c for _, parent in parents for c in parent]
            if any(ParentContext.boundary(c) != ParentContext.boundary(children[0]) for c in children):
                raise AppError("HIERARCHY_SCOPE_MISMATCH", "层级跨越来源边界", 503)
            root_id = node(1, root_text, children)
            nodes[-1]["node_metadata"].update(child_count=len(parents), descendant_leaf_count=len(children))
            for parent_text, parent in parents:
                parent_id = node(2, parent_text, parent, root_id)
                nodes[-1]["node_metadata"].update(child_count=len(parent), descendant_leaf_count=len(parent))
                for leaf in parent:
                    node(3, leaf.content, (), parent_id, leaf)
        if len(nodes) > len(leaves) * 3:
            raise AppError("HIERARCHY_INVALID", "层级节点数量异常", 503)
        return HierarchyTree(leaves, nodes, self.fingerprint, mode)
