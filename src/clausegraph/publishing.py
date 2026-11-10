"""Publish validated, immutable graph snapshots in Neo4j."""

import os
from collections import defaultdict

from .contracts import canonical, fingerprint
from .document_storage import write_tree
from .graphs import validate_fused
from .graph_style import document_label
from .neo4j_nodes import GRAPH_NODE_PROPERTIES

STORAGE_VERSION = 2
VERSION_FIELDS = ("document_id", "document_type", "source_sha256", "parser_version")


def _batches(items, size=500):
    for offset in range(0, len(items), size):
        yield items[offset:offset + size]


def neo4j_driver(uri):
    from neo4j import GraphDatabase

    password = os.getenv("NEO4J_PASSWORD")
    auth = (os.getenv("NEO4J_USER", "neo4j"), password) if password is not None else None
    return GraphDatabase.driver(uri, auth=auth, connection_timeout=5, max_transaction_retry_time=10)


def _document_identity(node):
    return tuple(node.properties.get(key) for key in ("document_id", "source_sha256", "parser_version"))


def _source_key(item):
    return item["unit"], item["start"], item["end"], item.get("row")


def _project_graph(graph, project_versions):
    indexed = {node.id: node for node in graph.nodes}
    documents = {node.id: node for node in graph.nodes if node.kind == "Document"}
    identities = {_document_identity(node): node.id for node in documents.values()}
    parents = {edge.target: edge.source for edge in graph.edges if edge.kind == "CONTAINS"}
    owners, sources, version_sources = {}, {}, defaultdict(list)
    nodes = defaultdict(list)
    for node in graph.nodes:
        props = node.properties
        fields = {"id": node.id, "kind": node.kind, "label": node.label}
        labels = []
        if node.kind in {"Document", "Clause", "ContentConcept"}:
            identity = _document_identity(node)
            owner = identities.get(identity)
            if owner is None:
                raise ValueError(f"节点没有所属文档：{node.id}")
            if node.kind != "Document":
                owners[node.id] = owner
            if node.kind in {"Document", "Clause"}:
                labels = [document_label(identity[0])]
        if node.kind == "Clause":
            parent = parents[node.id]
            if parent != owner and _document_identity(indexed[parent]) != identity:
                raise ValueError(f"条款层级与文档归属不一致：{node.id}")
            fields.update({key: props[key] for key in ("local_clause_id", "level", "order")})
            parts = props["sources"]
            if props["level"]:
                if not parts:
                    raise ValueError(f"条款缺少标题来源：{node.id}")
                location = parts[0]["location"]
                fields.update(location_unit=location["unit"], location_start=location["start"],
                              location_end=location["end"])
            for part in parts:
                entry = {**part["location"], "text": part["text"]}
                if part["row"] is not None:
                    entry["row"] = part["row"]
                sources[node.id, _source_key(entry)] = part["text"]
                if project_versions is None:
                    version_sources[owner].append({"local_clause_id": props["local_clause_id"], **entry})
        elif node.kind == "TypeConcept":
            fields.update({key: props[key] for key in ("uri", "labels", "document_types")})
        native = GRAPH_NODE_PROPERTIES[node.kind].model_validate(fields)
        nodes[node.kind].append({"id": node.id, "props": native.model_dump(exclude_none=True), "labels": labels})

    versions = []
    document_ids = {node.properties["document_id"] for node in documents.values()}
    if len(document_ids) != len(documents) or (project_versions is not None and set(project_versions) != document_ids):
        raise ValueError("快照文档与所选版本不一致")
    for node in documents.values():
        props = {key: node.properties.get(key) for key in VERSION_FIELDS}
        props["kind"] = "ClauseGraphDocumentVersion"
        if project_versions is None:
            entries = sorted(version_sources[node.id], key=canonical)
            version = fingerprint(["standalone", STORAGE_VERSION, *(props[key] for key in VERSION_FIELDS), entries])
            props["origin"] = "standalone"
        else:
            version = project_versions[props["document_id"]]
            props["origin"] = "project"
        versions.append({"id": version, "document": node.id, "props": props})

    standalone_trees = {}
    if project_versions is None:
        versions_by_document = {row["document"]: row["id"] for row in versions}
        standalone_trees = defaultdict(list)
        for node in graph.nodes:
            if node.kind != "Clause":
                continue
            props = node.properties
            parts = props["sources"]
            location = parts[0]["location"] if props["level"] else None
            blocks = {}
            for part in parts[1:] if location else parts:
                loc = part["location"]
                key = (loc["unit"], loc["start"], loc["end"])
                block = blocks.setdefault(key, {**loc, "order": len(blocks) + 1,
                                               "text": part["text"] if part["row"] is None else None, "rows": []})
                if part["row"] is not None:
                    block["rows"].append({"row": part["row"], "text": part["text"]})
            parent = indexed[parents[node.id]]
            standalone_trees[versions_by_document[owners[node.id]]].append({
                "id": props["local_clause_id"], "parent": parent.properties.get("local_clause_id"),
                "label": node.label, "level": props["level"], "order": props["order"],
                "location": location, "blocks": list(blocks.values()),
            })

    edges = defaultdict(list)
    for edge in graph.edges:
        kind = edge.kind
        if kind == "MENTIONS" and owners[edge.source] != owners[edge.target]:
            raise ValueError("内容概念与条款必须属于同一文档")
        props = {"predicate": edge.predicate or None}
        assertions = []
        for item in edge.properties.get("assertions", []):
            details = dict(item["properties"])
            if kind == "SIMILAR_TO":
                score, rank = details.pop("bm25"), details.pop("rank")
                if "rank" in props and (score, rank) != (props["bm25"], props["rank"]):
                    raise ValueError("同一条 BM25 边的分值和排名不一致")
                props.update(bm25=score, rank=rank)
            elif kind == "MENTIONS":
                details.pop("label", None)
            refs = []
            for evidence in item["evidence"]:
                clause = evidence["clause_id"]
                if kind == "CONTENT_RELATION" and owners[clause] != owners[edge.source]:
                    raise ValueError("内容关系证据必须属于同一文档")
                ref = {"clause_id": clause, **evidence["location"]}
                if evidence["row"] is not None:
                    ref["row"] = evidence["row"]
                excerpt = evidence["excerpt"]
                start = sources.get((clause, _source_key(ref)), "").find(excerpt)
                if start < 0:
                    raise ValueError(f"证据无法回查：{clause}")
                refs.append({**ref, "from": start, "to": start + len(excerpt)})
            assertions.append({"route": item["route"], "details": details, "evidence_refs": refs})
        if kind == "CONTENT_RELATION" and owners[edge.source] != owners[edge.target]:
            raise ValueError("内容关系不得跨文档")
        if assertions:
            props["assertions_json"] = canonical(assertions)
        edges[kind].append({"key": fingerprint(edge.key()), "source": edge.source, "target": edge.target,
                            "props": props})
    return nodes, edges, sorted(versions, key=lambda item: item["id"]), owners, standalone_trees


def publish_graph(graph, uri, *, database="neo4j", _project=None, _versions=None):
    validate_fused(graph)
    nodes, edges, versions, owners, standalone_trees = _project_graph(graph, _versions)
    graph_fingerprint = fingerprint(graph.normalized())
    snapshot = fingerprint([STORAGE_VERSION, graph_fingerprint, _project, [item["id"] for item in versions]])

    def write(tx):
        tx.run("""
            MERGE (s:ClauseGraphSnapshot {id:$snapshot})
            ON CREATE SET s.created_at=datetime(), s.kind='ClauseGraphSnapshot', s.status='ready',
                s.storage_schema_version=$schema, s.dataset_id=$dataset, s.metadata_json=$metadata,
                s.graph_fingerprint=$graph, s.base_fingerprint=$base, s.config_fingerprint=$config
            SET s.status='ready'
            """, snapshot=snapshot, schema=STORAGE_VERSION, dataset=graph.dataset_id,
               metadata=canonical(graph.metadata), graph=graph_fingerprint,
               base=graph.base_fingerprint, config=graph.config_fingerprint).consume()
        for kind, rows in nodes.items():
            for batch in _batches(rows):
                tx.run(f"""
                    MATCH (s:ClauseGraphSnapshot {{id:$snapshot}})
                    UNWIND $rows AS row
                    MERGE (n:ClauseGraphNode {{key:$snapshot + ':' + row.id}})
                    SET n:$(row.labels), n:{kind}, n.snapshot_id=$snapshot, n += row.props
                    MERGE (s)-[:HAS_NODE]->(n)
                    """, snapshot=snapshot, rows=batch).consume()
        if _project is None:
            tx.run("""
                UNWIND $rows AS row
                WITH row ORDER BY row.id
                MERGE (v:ClauseGraphDocumentVersion {id:row.id})
                ON CREATE SET v += row.props
                SET v.kind=row.props.kind
                """, rows=versions).consume()
        else:
            tx.run("""
                MATCH (p:ClauseGraphProject {id:$project}), (s:ClauseGraphSnapshot {id:$snapshot})
                MERGE (p)-[:HAS_SNAPSHOT]->(s)
                """, project=_project, snapshot=snapshot).consume()
        bound = tx.run("""
            MATCH (s:ClauseGraphSnapshot {id:$snapshot})
            UNWIND $rows AS row
            MATCH (v:ClauseGraphDocumentVersion {id:row.id}),
                  (d:Document {key:$snapshot + ':' + row.document})
            WHERE $project IS NULL OR (
                EXISTS { MATCH (:ClauseGraphProject {id:$project})-[:HAS_VERSION]->(v) }
                AND v.document_id = row.props.document_id AND v.source_sha256 = row.props.source_sha256
                AND coalesce(v.document_type = row.props.document_type,
                             v.document_type IS NULL AND row.props.document_type IS NULL)
                AND v.parser_version = row.props.parser_version AND v.origin = 'project'
                AND EXISTS { MATCH (v)-[:HAS_ROOT]->(:VersionClause) }
            )
            WITH s, d, v, row ORDER BY row.id
            MERGE (s)-[:USES_DOCUMENT]->(v)
            MERGE (d)-[:REPRESENTS_VERSION]->(v)
            RETURN count(v) AS bound
            """, snapshot=snapshot, project=_project, rows=versions).single(strict=True)
        if bound["bound"] != len(versions):
            raise ValueError("快照文件版本不存在或与总图不一致")
        for version, rows in standalone_trees.items():
            write_tree(tx, version, rows)
        for batch in _batches(list(owners.items())):
            tx.run("""
                UNWIND $rows AS row
                MATCH (n:ClauseGraphNode {key:$snapshot + ':' + row[0]}),
                      (d:ClauseGraphNode {key:$snapshot + ':' + row[1]})
                MERGE (n)-[:IN_DOCUMENT]->(d)
                """, snapshot=snapshot, rows=batch).consume()
        tx.run("""
            MATCH (:ClauseGraphSnapshot {id:$snapshot})-[:HAS_NODE]->(c:Clause)
                  -[:IN_DOCUMENT]->(:Document)-[:REPRESENTS_VERSION]->(v)
            MATCH (original:VersionClause {version_id:v.id, local_clause_id:c.local_clause_id})
            MERGE (c)-[:REPRESENTS_CLAUSE]->(original)
            """, snapshot=snapshot).consume()
        for kind, rows in edges.items():
            for batch in _batches(rows):
                tx.run(f"""
                    UNWIND $rows AS row
                    MATCH (a:ClauseGraphNode {{key:$snapshot + ':' + row.source}}),
                          (b:ClauseGraphNode {{key:$snapshot + ':' + row.target}})
                    MERGE (a)-[r:{kind} {{key:row.key}}]->(b)
                    SET r += row.props
                    """, snapshot=snapshot, rows=batch).consume()
        record = tx.run("""
            MATCH (s:ClauseGraphSnapshot {id:$snapshot})
            CALL (s) {
                MATCH (s)-[:HAS_NODE]->(n) RETURN count(n) AS nodes
            }
            CALL (s) {
                MATCH (s)-[:HAS_NODE]->(a)-[r]->(b:ClauseGraphNode {snapshot_id:$snapshot})
                WHERE type(r) <> 'IN_DOCUMENT'
                RETURN count(r) AS edges
            }
            RETURN nodes, edges
            """, snapshot=snapshot).single(strict=True)
        if (record["nodes"], record["edges"]) != (len(graph.nodes), len(graph.edges)):
            raise RuntimeError("Neo4j 节点或边计数与总图不一致")
        return {"snapshot_id": snapshot, "nodes": record["nodes"], "edges": record["edges"]}

    with neo4j_driver(uri) as driver, driver.session(database=database) as session:
        for name, label, key in (
            ("snapshot", "ClauseGraphSnapshot", "id"), ("node", "ClauseGraphNode", "key"),
            ("document_version", "ClauseGraphDocumentVersion", "id"),
        ):
            session.run(f"CREATE CONSTRAINT clause_graph_{name} IF NOT EXISTS "
                        f"FOR (n:{label}) REQUIRE n.{key} IS UNIQUE").consume()
        for name, label, keys in (
            ("version_document", "ClauseGraphDocumentVersion", "n.document_id"),
            ("concept_uri", "TypeConcept", "n.uri"),
            ("node_kind", "ClauseGraphNode", "n.snapshot_id, n.kind"),
            ("version_clause", "VersionClause", "n.version_id, n.local_clause_id"),
        ):
            session.run(f"CREATE INDEX clause_graph_{name} IF NOT EXISTS "
                        f"FOR (n:{label}) ON ({keys})").consume()
        return session.execute_write(write)
