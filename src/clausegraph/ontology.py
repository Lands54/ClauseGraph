"""Compile caller-supplied RDF profiles and attach clauses with source evidence."""

import re
from pathlib import Path

from rdflib import Graph as RDFGraph, Namespace, RDF, RDFS, URIRef
from rdflib.compare import to_isomorphic

from .contracts import Edge, Evidence, Location, Node
from .graphs import route_graph

R2 = Namespace("https://codex.local/route2/vocab#")
GROUPS = {
    "title": (R2.matchTitleCue, R2.weightTitle, 0.4),
    "body": (R2.matchBodyCue, R2.weightBody, 0.2),
    "header": (R2.matchHeaderCue, R2.weightHeader, 0.35),
    "identifier": (R2.matchIdentifierPattern, R2.weightIdentifier, 0.2),
}
SETTINGS = {R2.alias, R2.allowedDocumentType, R2.scoreFloor}
SETTINGS.update(predicate for cue, weight, _ in GROUPS.values() for predicate in (cue, weight))


def _one(graph, subject, predicate, default, convert, low, high):
    values = list(graph.objects(subject, predicate))
    if len(values) > 1:
        raise ValueError(f"{subject} 的 {predicate} 存在多个值")
    value = convert(str(values[0])) if values else default
    if not low <= value <= high:
        raise ValueError(f"{subject} 的 {predicate} 必须在 {low} 至 {high} 之间")
    return value


def _strings(graph, subject, predicate):
    return sorted({str(value) for value in graph.objects(subject, predicate) if str(value).strip()})


def _normalize(text):
    return re.sub(r"\s+", "", text).casefold()


def load_ontology(path):
    """Load and validate a Turtle profile without fetching remote data."""
    return compile_ontology(Path(path).read_bytes())


def compile_ontology(data):
    graph = RDFGraph().parse(data=data, format="turtle", publicID="urn:clausegraph:ontology:")
    headers = set(graph.subjects(R2.version, None))
    if len(headers) > 1:
        raise ValueError("本体只能有一个版本配置节点")
    header = next(iter(headers), URIRef("urn:clausegraph:defaults"))
    if headers and len(_strings(graph, header, R2.version)) != 1:
        raise ValueError("本体版本必须为单个非空值")
    threshold = _one(graph, header, R2.confirmedScoreFloor, 0.72, float, 0, 1)
    min_groups = _one(graph, header, R2.confirmedEvidenceGroupFloor, 2, int, 1, 4)
    subjects = set(graph.subjects(RDF.type, R2.Concept))
    for predicate in SETTINGS | {RDFS.label}:
        subjects.update(graph.subjects(predicate, None))
    subjects.difference_update(headers)
    if not subjects or any(not isinstance(subject, URIRef) for subject in subjects):
        raise ValueError("本体须包含使用绝对 URI 的命名概念")
    profiles = {}
    for subject in sorted(subjects, key=str):
        labels = _strings(graph, subject, RDFS.label)
        if not labels:
            raise ValueError(f"概念缺少 rdfs:label：{subject}")
        cues = {key: _strings(graph, subject, cue) for key, (cue, _, _) in GROUPS.items()}
        try:
            patterns = [re.compile(pattern, re.IGNORECASE) for pattern in cues["identifier"]]
        except re.error as exc:
            raise ValueError(f"概念 {subject} 的编号规则无效：{exc}") from exc
        profiles[subject] = {
            "label": labels[0], "labels": labels,
            "types": _strings(graph, subject, R2.allowedDocumentType),
            "aliases": _strings(graph, subject, R2.alias), "cues": cues, "patterns": patterns,
            "weights": {key: _one(graph, subject, weight, default, float, 0, 1)
                        for key, (_, weight, default) in GROUPS.items()},
            "floor": _one(graph, subject, R2.scoreFloor, 0.4, float, 0, 1),
        }
    relations = []
    for source, predicate, target in graph:
        if source in profiles and isinstance(target, URIRef) and predicate not in SETTINGS | {RDF.type, RDFS.label}:
            if target not in profiles:
                raise ValueError(f"本体关系引用未声明概念：{source} → {target}")
            relations.append((source, predicate, target))
    return str(to_isomorphic(graph).graph_digest()), profiles, sorted(relations), threshold, min_groups


def _matching_parts(parts, profile):
    evidence = {}
    for item in parts:
        value = item["text"]
        normalized = _normalize(value)
        group = "title" if item["title"] else "body"
        groups = {group: profile["cues"][group] + profile["aliases"]}
        if item["row"] == 1:
            groups["header"] = profile["cues"]["header"]
        for name, needles in groups.items():
            hits = [needle for needle in needles if _normalize(needle) in normalized]
            if hits:
                evidence.setdefault(name, []).append((item, hits))
        matches = [match.group() for pattern in profile["patterns"] for match in pattern.finditer(value)
                   if match.group()]
        if matches:
            evidence.setdefault("identifier", []).append((item, matches))
    return evidence


def build_route2(base, ontology):
    return _build_route2(base, load_ontology(ontology))


def _build_route2(base, config):
    digest, profiles, relations, threshold, min_groups = config
    ids = {uri: "type:" + str(uri) for uri in profiles}
    nodes = [Node(id=ids[uri], kind="TypeConcept", label=profile["label"],
                  properties={"uri": str(uri), "labels": profile["labels"], "document_types": profile["types"]})
             for uri, profile in profiles.items()]
    edges = [Edge(source=ids[left], target=ids[right], kind="TYPE_RELATION", predicate=str(predicate))
             for left, predicate, right in relations]
    unmatched = []
    for node in sorted(base.nodes, key=lambda node: node.id):
        if node.kind != "Clause":
            continue
        parts = [{**item, "title": node.properties["level"] > 0 and index == 0}
                 for index, item in enumerate(node.properties["sources"]) if item["text"].strip()]
        if not parts:
            continue
        matched = False
        for uri, profile in profiles.items():
            if profile["types"] and node.properties["document_type"] not in profile["types"]:
                continue
            hits = _matching_parts(parts, profile)
            score = min(1.0, sum(profile["weights"][key] for key in hits))
            if not hits or score < profile["floor"]:
                continue
            matched = True
            evidence = {}
            for group in hits.values():
                for item, _ in group:
                    location = Location.model_validate(item["location"])
                    evidence[location.start, location.end, item["row"]] = Evidence(
                        clause_id=node.id, location=location, row=item["row"], excerpt=item["text"])
            edges.append(Edge(source=node.id, target=ids[uri], kind="CLASSIFIED_AS",
                              evidence=list(evidence.values()), properties={
                                  "rule_score": score,
                                  "review_status": "confirmed" if score >= threshold and len(hits) >= min_groups else "review_required",
                                  "matched_cues": {key: sorted({cue for _, values in group for cue in values})
                                                   for key, group in hits.items()},
                              }))
        if not matched:
            unmatched.append(node.id)
    return route_graph(base, "route2", {"ontology": digest, "rule_engine": 1},
                       nodes=nodes, edges=edges, metadata={"unmatched_clauses": unmatched})
