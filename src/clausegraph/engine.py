"""Manifest validation and local artifact orchestration."""

import json
import os
import tempfile
from pathlib import Path

from pydantic import Field, model_validator

from .bm25 import build_route1
from .contracts import Contract, Name
from .extraction import ExternalModelSettings, OntoRAGExternal, build_route3
from .graphs import build_base, fuse_graphs, route_graph
from .ontology import _build_route2, load_ontology
from .parsing import parse_document
from .publishing import publish_graph


class DocumentInput(Contract):
    id: Name
    path: Name
    type: Name | None = None


class Manifest(Contract):
    schema_version: int = Field(default=1, ge=1, le=1)
    dataset_id: Name
    documents: list[DocumentInput] = Field(min_length=1)
    ontology: Name
    enable_route3: bool = False
    model: ExternalModelSettings = Field(default_factory=ExternalModelSettings)
    top_k: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def unique_documents(self):
        if len({item.id for item in self.documents}) != len(self.documents):
            raise ValueError("清单中的文档 ID 不可重复")
        return self


class RunError(RuntimeError):
    """The run failed; consult report.json in the run directory."""


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def run(manifest, output, *, provider=None, enable_route3: bool | None = None, neo4j_uri=None, database="neo4j"):
    """Run from a JSON manifest. The output must be a new or empty directory."""
    path = Path(manifest).resolve()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    lock = output / ".running"
    with lock.open("x"):
        pass
    try:
        if any(item != lock for item in output.iterdir()):
            raise ValueError("输出目录必须为空，请为本次构建指定新目录")
        return await _run(path, output, provider, enable_route3, neo4j_uri, database)
    finally:
        lock.unlink(missing_ok=True)


async def _run(path, output, provider, enable_route3, neo4j_uri, database):
    report = {"schema_version": 1, "status": "failed", "documents": [], "graphs": {}}
    stage = "configuration"

    def save_graph(name, graph):
        write_json(output / f"{name}.json", graph)
        report["graphs"][name] = {"status": graph.status, "nodes": len(graph.nodes), "edges": len(graph.edges),
                                  "config_fingerprint": graph.config_fingerprint, **graph.metadata}

    try:
        config = Manifest.model_validate_json(path.read_bytes())
        report["dataset_id"] = config.dataset_id
        ontology = load_ontology(path.parent / config.ontology)
        route3_enabled = config.enable_route3 if enable_route3 is None else enable_route3
        report["enable_route3"] = route3_enabled
        if route3_enabled and provider is None:
            provider = OntoRAGExternal(config.model)
        stage = "parse"
        trees = []
        for index, document in enumerate(config.documents, 1):
            tree = parse_document(path.parent / document.path, document.id, document.type)
            artifact = f"trees/{index:04d}.json"
            write_json(output / artifact, tree)
            report["documents"].append({"id": tree.document_id, "sha256": tree.source_sha256,
                                        "parser_version": tree.parser_version, "artifact": artifact,
                                        "diagnostics": tree.diagnostics})
            trees.append(tree)
        stage = "base"
        base = build_base(config.dataset_id, trees)
        save_graph("base", base)
        stage = "route1"
        first = build_route1(base, top_k=config.top_k)
        save_graph(stage, first)
        stage = "route2"
        second = _build_route2(base, ontology)
        save_graph(stage, second)
        stage = "route3"
        if route3_enabled:
            third = await build_route3(base, provider)
        else:
            third = route_graph(base, "route3", {"enabled": False}, status="skipped")
        save_graph(stage, third)
        stage = "fuse"
        fused = fuse_graphs(base, [first, second, third])
        save_graph("fused", fused)
        if fused.status != "ready":
            stage = "route3"
            raise RunError("内容本体抽取未全部完成，详见 failed_units")
        if neo4j_uri:
            stage = "publish"
            report["publication"] = publish_graph(fused, neo4j_uri, database=database)
        else:
            report["publication"] = {"status": "local_only"}
        report["status"] = "ready"
    except Exception as exc:
        report["failure"] = {"stage": stage, "message": str(exc)}
        raise RunError(f"{stage}：{exc}（{output / 'report.json'}）") from exc
    finally:
        write_json(output / "report.json", report)
    return report
