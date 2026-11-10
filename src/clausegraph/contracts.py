"""Versioned, portable contracts shared by parsers and graph builders."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, Field(min_length=1)]
Index = Annotated[int, Field(ge=1)]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Location(Contract):
    unit: Literal["docx_block", "markdown_line"]
    start: Index
    end: Index

    @model_validator(mode="after")
    def check_range(self):
        if self.end < self.start or (self.unit == "docx_block" and self.end != self.start):
            raise ValueError("无效来源范围")
        return self


class Block(Contract):
    kind: Literal["paragraph", "table"]
    location: Location
    text: str = ""
    rows: list[list[str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_content(self):
        if self.kind == "table":
            if self.text or not self.rows or any(not row for row in self.rows):
                raise ValueError("表格必须包含行，且不能同时有段落文本")
        elif self.rows:
            raise ValueError("段落不能包含表格行")
        return self


class Clause(Contract):
    clause_id: Name
    title: str
    level: int = Field(ge=0)
    order: int = Field(ge=0)
    location: Location | None = None
    blocks: list[Block] = Field(default_factory=list)
    children: list["Clause"] = Field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class ClauseTree(Contract):
    schema_version: Literal[1] = 1
    document_id: Name
    source_sha256: Digest
    parser_version: Name
    document_type: Name | None = None
    root: Clause
    diagnostics: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_tree(self):
        if self.root.level or self.root.order or self.root.location or self.root.title or not self.root.children:
            raise ValueError("条款树必须包含无标题根节点和至少一个标题")
        seen = set()
        last_order = -1
        last_end = 0
        unit = None
        for node in self.root.walk():
            if node.clause_id in seen or node.order <= last_order:
                raise ValueError("条款 ID 重复或条款顺序无效")
            seen.add(node.clause_id)
            last_order = node.order
            if node is not self.root and (not node.location or not node.title.strip()):
                raise ValueError("条款缺少标题或来源")
            locations = [block.location for block in node.blocks]
            if node.location:
                locations.insert(0, node.location)
            for loc in locations:
                unit = unit or loc.unit
                if loc.unit != unit or loc.start <= last_end:
                    raise ValueError("来源类型不一致或内容块顺序重叠")
                last_end = loc.end
            if any(child.level <= node.level for child in node.children):
                raise ValueError("子条款层级必须高于父条款")
        return self


class Evidence(Contract):
    clause_id: Name
    location: Location
    row: Index | None = None
    excerpt: Name


class Node(Contract):
    id: Name
    kind: Literal["Document", "Clause", "TypeConcept", "ContentConcept"]
    label: str
    properties: dict[str, JsonValue] = Field(default_factory=dict)


class Edge(Contract):
    source: Name
    target: Name
    kind: Literal["CONTAINS", "SIMILAR_TO", "CLASSIFIED_AS", "MENTIONS", "TYPE_RELATION", "CONTENT_RELATION"]
    predicate: str = ""
    properties: dict[str, JsonValue] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)

    def key(self):
        return self.kind, self.source, self.target, self.predicate


class Graph(Contract):
    schema_version: Literal[2] = 2
    dataset_id: Name
    base_fingerprint: Digest
    config_fingerprint: Digest
    route: Literal["base", "route1", "route2", "route3", "fused"]
    status: Literal["ready", "partial", "failed", "skipped"] = "ready"
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    def normalized(self):
        data = self.model_dump(mode="json")
        data["nodes"].sort(key=lambda node: node["id"])
        for edge in data["edges"]:
            edge["evidence"].sort(key=canonical)
        data["edges"].sort(key=canonical)
        return data


def clause_key(tree, clause):
    return "clause:" + fingerprint([tree.document_id, tree.source_sha256, tree.parser_version, clause.clause_id])


def sources(node):
    if node.location:
        yield node.location, None, node.title
    for block in node.blocks:
        if block.kind == "paragraph":
            yield block.location, None, block.text
        else:
            for row, cells in enumerate(block.rows, 1):
                yield block.location, row, " | ".join(cells)
