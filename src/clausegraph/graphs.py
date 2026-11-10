"""Build the common graph and verify route assertions before merging."""

from .contracts import Edge, Evidence, Graph, Location, Node, canonical, clause_key, fingerprint, sources

ROUTES = ("route1", "route2", "route3")
KINDS = {
    "route1": {"SIMILAR_TO": ("Clause", "Clause")},
    "route2": {"CLASSIFIED_AS": ("Clause", "TypeConcept"), "TYPE_RELATION": ("TypeConcept", "TypeConcept")},
    "route3": {"MENTIONS": ("Clause", "ContentConcept"), "CONTENT_RELATION": ("ContentConcept", "ContentConcept")},
}
OWNERS = {"route2": ("TypeConcept", "type:"), "route3": ("ContentConcept", "content:")}
BASE_CONFIG = fingerprint({"base": 1})


def build_base(dataset_id, trees):
    trees = list(trees)
    ids = {tree.document_id for tree in trees}
    if not trees or len(ids) != len(trees):
        raise ValueError("必须提供至少一棵条款树，文档 ID 不可重复")
    nodes, edges = [], []
    for tree in trees:
        common = {"document_id": tree.document_id, "source_sha256": tree.source_sha256,
                  "parser_version": tree.parser_version, "document_type": tree.document_type}
        doc_id = "document:" + fingerprint(common)
        nodes.append(Node(id=doc_id, kind="Document", label=tree.document_id, properties=common))
        edges.append(Edge(source=doc_id, target=clause_key(tree, tree.root), kind="CONTAINS"))
        for clause in tree.root.walk():
            key = clause_key(tree, clause)
            parts = [{"location": loc.model_dump(), "row": row, "text": value}
                     for loc, row, value in sources(clause)]
            properties = {**common, "local_clause_id": clause.clause_id, "level": clause.level,
                          "order": clause.order, "sources": parts}
            nodes.append(Node(id=key, kind="Clause", label=clause.title, properties=properties))
            edges.extend(Edge(source=key, target=clause_key(tree, child), kind="CONTAINS")
                         for child in clause.children)
    graph = Graph(dataset_id=dataset_id, base_fingerprint=BASE_CONFIG,
                  config_fingerprint=BASE_CONFIG, route="base", nodes=nodes, edges=edges)
    return graph.model_copy(update={"base_fingerprint": fingerprint(graph.normalized())})


def route_graph(base, route, config, **values):
    return Graph(dataset_id=base.dataset_id, base_fingerprint=base.base_fingerprint,
                 config_fingerprint=fingerprint(config), route=route, **values)


def _node_index(nodes):
    indexed = {node.id: node for node in nodes}
    if len(indexed) != len(nodes):
        raise ValueError("节点 ID 重复")
    return indexed


def validate_base(base):
    if (base.route != "base" or base.status != "ready" or base.config_fingerprint != BASE_CONFIG
        or fingerprint(base.model_copy(update={"base_fingerprint": BASE_CONFIG}).normalized()) != base.base_fingerprint):
        raise ValueError("公共底图指纹或状态无效")
    nodes = _node_index(base.nodes)
    parents = {}
    for edge in base.edges:
        source, target = nodes.get(edge.source), nodes.get(edge.target)
        if (not source or not target or edge.kind != "CONTAINS" or edge.predicate or edge.properties
            or edge.evidence or target.kind != "Clause" or target.id in parents):
            raise ValueError("公共底图层级边无效")
        if any(source.properties.get(key) != target.properties.get(key)
               for key in ("document_id", "source_sha256", "parser_version", "document_type")):
            raise ValueError("公共底图层级跨文档版本")
        parents[target.id] = source.id
    for node in nodes.values():
        if node.kind == "Document" and node.id.startswith("document:"):
            roots = [edge for edge in base.edges if edge.source == node.id]
            if len(roots) == 1 and nodes[roots[0].target].properties.get("level") == 0:
                continue
        elif node.kind == "Clause" and node.id.startswith("clause:") and node.id in parents:
            parent = nodes[parents[node.id]]
            level = node.properties.get("level")
            if type(level) is int and level >= 0 and (
                (parent.kind == "Document" and level == 0)
                or (parent.kind == "Clause" and type(parent.properties.get("level")) is int
                    and parent.properties["level"] < level)
            ):
                continue
        raise ValueError(f"公共底图节点无效：{node.id}")
    return nodes


def _validate_route(base, route, public, evidence_sources):
    if route.dataset_id != base.dataset_id or route.base_fingerprint != base.base_fingerprint:
        raise ValueError("路线与公共底图不一致")
    if route.status == "skipped" and route.route != "route3":
        raise ValueError("只允许关闭三路")
    if route.status in {"failed", "skipped"} and (route.nodes or route.edges):
        raise ValueError("失败或关闭的路线图必须为空")
    local = _node_index(route.nodes)
    if local:
        kind, prefix = OWNERS.get(route.route, ("", ""))
        if any(node.kind != kind or not node.id.startswith(prefix) or node.id in public for node in local.values()):
            raise ValueError("路线节点冲突或命名空间无效")
    available = public | local
    for edge in route.edges:
        left, right = available.get(edge.source), available.get(edge.target)
        if not left or not right or KINDS[route.route].get(edge.kind) != (left.kind, right.kind):
            raise ValueError("边端点不存在或类型无效")
        if edge.kind == "SIMILAR_TO" and left.id == right.id:
            raise ValueError("相似边不能指向自身")
        if edge.kind != "TYPE_RELATION" and not edge.evidence:
            raise ValueError("断言缺少原文证据")
        for item in edge.evidence:
            excerpt = evidence_sources.get((item.clause_id, item.location.unit, item.location.start,
                                            item.location.end, item.row), "")
            if item.excerpt not in excerpt:
                raise ValueError(f"证据无法回查：{item.clause_id}")
            if edge.kind in {"SIMILAR_TO", "CLASSIFIED_AS", "MENTIONS"} and item.clause_id not in {left.id, right.id}:
                raise ValueError("证据与断言端点无关")
        if edge.kind == "SIMILAR_TO" and {item.clause_id for item in edge.evidence} != {left.id, right.id}:
            raise ValueError("相似边缺少双侧证据")
    return local


def _evidence_index(public):
    indexed = {}
    for node in public.values():
        for item in node.properties.get("sources", []):
            location = Location.model_validate(item["location"])
            key = (node.id, location.unit, location.start, location.end, item["row"])
            if node.kind != "Clause" or key in indexed or not isinstance(item["text"], str):
                raise ValueError("公共底图证据来源无效")
            indexed[key] = item["text"]
    return indexed


def fuse_graphs(base: Graph, routes) -> Graph:
    public = validate_base(base)
    routes = sorted(routes, key=lambda item: item.route)
    if tuple(route.route for route in routes) != ROUTES:
        raise ValueError("融合必须提供三个独立路线图")
    evidence_sources = _evidence_index(public)
    nodes = public.copy()
    grouped = {}
    descriptors = {}
    for route in routes:
        nodes.update(_validate_route(base, route, public, evidence_sources))
        descriptors[route.route] = {"status": route.status, "config_fingerprint": route.config_fingerprint,
                                    "metadata": route.metadata}
        for edge in route.edges:
            key = edge.key()
            assertion = {"route": route.route, **edge.model_dump(mode="json")}
            assertion["evidence"].sort(key=canonical)
            grouped.setdefault(key, {})[canonical(assertion)] = assertion
    edges = list(base.edges)
    for (kind, source, target, predicate), assertions in sorted(grouped.items()):
        items = [assertions[key] for key in sorted(assertions)]
        evidence = {canonical(item): item for assertion in items for item in assertion["evidence"]}
        edges.append(Edge(kind=kind, source=source, target=target, predicate=predicate,
                          properties={"assertions": items},
                          evidence=[Evidence.model_validate(evidence[key]) for key in sorted(evidence)]))
    return route_graph(base, "fused", {name: item["config_fingerprint"] for name, item in descriptors.items()},
                       status="ready" if all(route.status in {"ready", "skipped"} for route in routes) else "partial",
                       nodes=sorted(nodes.values(), key=lambda node: node.id),
                       edges=sorted(edges, key=Edge.key), metadata={"routes": descriptors})


def validate_fused(graph):
    """Reconstruct the input assertions to validate a standalone publish artifact."""
    if graph.route != "fused" or graph.status != "ready":
        raise ValueError("只有已开启路线均完成的总图可发布")
    base = Graph(dataset_id=graph.dataset_id, route="base", base_fingerprint=graph.base_fingerprint,
                 config_fingerprint=BASE_CONFIG,
                 nodes=[node for node in graph.nodes if node.kind in {"Document", "Clause"}],
                 edges=[edge for edge in graph.edges if edge.kind == "CONTAINS"])
    try:
        assertions = {route: [] for route in ROUTES}
        for edge in graph.edges:
            if edge.kind != "CONTAINS":
                for assertion in edge.properties["assertions"]:
                    data = dict(assertion)
                    assertions[data.pop("route")].append(Edge.model_validate(data))
        routes = []
        for route in ROUTES:
            data = graph.metadata["routes"][route]
            kind = OWNERS.get(route, (None, ""))[0]
            routes.append(Graph(dataset_id=graph.dataset_id, route=route, base_fingerprint=graph.base_fingerprint,
                                nodes=[node for node in graph.nodes if node.kind == kind],
                                edges=assertions[route], **data))
        if fuse_graphs(base, routes).normalized() != graph.normalized():
            raise ValueError("总图与原始断言不一致")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("总图缺少有效的路线断言或运行状态") from exc
