"""Project file versions and independently selected graph snapshots."""

import json
from pathlib import Path

from .bm25 import build_route1
from .contracts import Graph, canonical, fingerprint
from .document_storage import read_tree, store_tree
from .engine import write_json
from .extraction import EXTRACTOR_VERSION, ExternalModelSettings, OntoRAGExternal, build_route3
from .graphs import build_base, fuse_graphs, route_graph
from .ontology import _build_route2, compile_ontology
from .parsing import parse_document
from .publishing import neo4j_driver, publish_graph


def init_project(project, ontology, *, neo4j_uri, database="neo4j", enable_route3=False, model=None):
    if not project.strip():
        raise ValueError("项目 ID 不能为空")
    source = Path(ontology).read_text(encoding="utf-8")
    compile_ontology(source)
    settings = model or ExternalModelSettings()
    config = {"enable_route3": enable_route3, "model": settings.model_dump(mode="json")}

    def create(tx):
        row = tx.run("""
            MERGE (p:ClauseGraphProject {id:$project})
            ON CREATE SET p.ontology=$ontology, p.enable_route3=$enabled, p.model_config_json=$model
            SET p.kind='ClauseGraphProject', p.revision=coalesce(p.revision, 0)
            RETURN p.enable_route3 AS enabled,
                   p.model_config_json AS model, p.ontology AS ontology
        """, project=project, enabled=enable_route3, model=canonical(config["model"]),
               ontology=source).single(strict=True)
        if _project_config(row) != config or row["ontology"] != source:
            raise ValueError("该项目已存在，配置不同")

    with neo4j_driver(neo4j_uri) as driver, driver.session(database=database) as session:
        session.run("CREATE CONSTRAINT clause_graph_project IF NOT EXISTS "
                    "FOR (p:ClauseGraphProject) REQUIRE p.id IS UNIQUE").consume()
        session.run("CREATE CONSTRAINT clause_graph_document_version IF NOT EXISTS "
                    "FOR (d:ClauseGraphDocumentVersion) REQUIRE d.id IS UNIQUE").consume()
        session.run("CREATE INDEX clause_graph_version_clause IF NOT EXISTS "
                    "FOR (c:VersionClause) ON (c.version_id, c.local_clause_id)").consume()
        session.execute_write(create)
    return {"project": project}


def _project_config(row):
    return {"enable_route3": row["enabled"], "model": json.loads(row["model"])}


def _read_project(project, uri, database, *, history=False):
    with neo4j_driver(uri) as driver, driver.session(database=database) as session:
        row = session.run("""
            MATCH (p:ClauseGraphProject {id:$project})
            OPTIONAL MATCH (p)-[r:HAS_VERSION]->(d:ClauseGraphDocumentVersion)
            WHERE $history OR r.retired_at IS NULL
            RETURN p.enable_route3 AS enabled,
                   p.model_config_json AS model, p.ontology AS ontology,
                   collect(CASE WHEN d IS NOT NULL THEN
                     d{.id,.document_id,.document_type,.source_sha256,.route2_json,.route3_json,
                       .route3_status, active:r.retired_at IS NULL}
                   END) AS files
        """, project=project, history=history).single()
    if row is None:
        raise ValueError(f"项目尚未初始化：{project}")
    return row


def list_project_files(project, *, neo4j_uri, database="neo4j", history=False):
    row = _read_project(project, neo4j_uri, database, history=history)
    fields = ("id", "document_id", "document_type", "source_sha256", "route3_status", "active")
    return [{key: item[key] for key in fields}
            for item in sorted(row["files"], key=lambda item: (item["document_id"], item["id"]))]


async def import_document(project, path, document_id, *, document_type=None, replace=False,
                          enable_route3=None, provider=None, neo4j_uri, database="neo4j"):
    state = _read_project(project, neo4j_uri, database)
    config = _project_config(state)
    active = next((item for item in state["files"] if item["document_id"] == document_id), None)
    if document_type is None and active:
        document_type = active["document_type"]
    tree = parse_document(path, document_id, document_type)
    base = build_base(project, [tree])
    enabled = config["enable_route3"] if enable_route3 is None else enable_route3
    if enabled and provider is None:
        provider = OntoRAGExternal(ExternalModelSettings.model_validate(config["model"]))
    tree_data = tree.model_dump(mode="json")
    version = fingerprint([project, tree_data, state["ontology"], [EXTRACTOR_VERSION, provider.configuration] if enabled else None])
    previous = active["id"] if active else None
    if version == previous:
        return {"status": "unchanged", "version_id": version, "route2": "ready", "route3": active["route3_status"]}
    if previous and not replace:
        raise ValueError("该文档已有活动版本，使用 --replace 提交新版本")

    second = _build_route2(base, compile_ontology(state["ontology"]))
    third = (await build_route3(base, provider) if enabled
             else route_graph(base, "route3", {"enabled": False}, status="skipped"))
    if third.status not in {"ready", "skipped"}:
        raise ValueError(f"三路未完成，失败单元数：{len(third.metadata.get('failed_units', []))}")

    def save(tx):
        row = tx.run("""
            MATCH (p:ClauseGraphProject {id:$project})
            SET p.revision=p.revision+1
            WITH p
            OPTIONAL MATCH (p)-[r:HAS_VERSION]->(d:ClauseGraphDocumentVersion {document_id:$document})
            WHERE r.retired_at IS NULL
            RETURN d.id AS current
        """, project=project, document=document_id).single(strict=True)
        if row["current"] != previous:
            raise ValueError("文档已被其他操作更新，请重试")
        tx.run("""
            MATCH (p:ClauseGraphProject {id:$project})
            OPTIONAL MATCH (p)-[old:HAS_VERSION]->(:ClauseGraphDocumentVersion {id:$previous})
            SET old.retired_at=datetime()
            WITH p
            MERGE (d:ClauseGraphDocumentVersion {id:$version})
            ON CREATE SET d.document_id=$document,
                          d.document_type=$type, d.source_sha256=$sha,
                          d.route2_json=$second, d.route3_json=$third, d.route3_status=$status
            SET d.kind='ClauseGraphDocumentVersion', d.origin='project', d.parser_version=$parser
            MERGE (p)-[r:HAS_VERSION]->(d)
            REMOVE r.retired_at
        """, project=project, previous=previous, version=version, document=document_id,
               type=tree.document_type, sha=tree.source_sha256, parser=tree.parser_version,
               second=second.model_dump_json(), third=third.model_dump_json(), status=third.status).consume()
        store_tree(tx, version, tree)

    with neo4j_driver(neo4j_uri) as driver, driver.session(database=database) as session:
        session.execute_write(save)
    return {"status": "ready", "version_id": version, "replaced_version_id": previous,
            "route2": "ready", "route3": third.status}


def _assemble_route(base, route, files):
    nodes = {}
    edges = []
    configs = {}
    statuses = {}
    for file in files:
        graph = Graph.model_validate_json(file[f"{route}_json"])
        allowed = {"ready"} if route == "route2" else {"ready", "skipped"}
        if graph.route != route or graph.dataset_id != base.dataset_id or graph.status not in allowed:
            raise ValueError("文件路线产物无效")
        for node in graph.nodes:
            if node.id in nodes and nodes[node.id] != node:
                raise ValueError(f"概念节点冲突：{node.id}")
            nodes[node.id] = node
        edges.extend(graph.edges)
        configs[file["id"]] = graph.config_fingerprint
        statuses[file["id"]] = graph.status
    if route == "route2" and len(set(configs.values())) > 1:
        raise ValueError("所选版本使用了不同的二路本体")
    status = "skipped" if all(value == "skipped" for value in statuses.values()) else "ready"
    return route_graph(base, route, configs, status=status, nodes=list(nodes.values()), edges=edges,
                       metadata={"version_statuses": statuses})


def create_snapshot(project, *, version_ids=None, all_active=False, top_k=None, output=None,
                    neo4j_uri, database="neo4j"):
    if bool(version_ids) == all_active:
        raise ValueError("请选择版本 ID 或 --all-active")
    state = _read_project(project, neo4j_uri, database)
    available = {item["id"]: item for item in state["files"]}
    ids = sorted(available if all_active else version_ids)
    if not ids or len(set(ids)) != len(ids) or any(key not in available for key in ids):
        raise ValueError("请选择本项目中不重复的活动文件版本")
    files = [available[key] for key in ids]
    with neo4j_driver(neo4j_uri) as driver, driver.session(database=database) as session:
        trees = session.execute_read(lambda tx: [read_tree(tx, file["id"]) for file in files])
    base = build_base(project, trees)
    first = build_route1(base, top_k=top_k)
    fused = fuse_graphs(base, [first, *(_assemble_route(base, route, files) for route in ("route2", "route3"))])
    if output is not None:
        write_json(output, fused)

    versions = {item["document_id"]: item["id"] for item in files}
    publication = publish_graph(fused, neo4j_uri, database=database, _project=project,
                                _versions=versions)
    return {**publication, "version_ids": ids}
