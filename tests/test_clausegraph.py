import asyncio
import json
import importlib.util
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4
from zipfile import ZipFile

from clausegraph import (
    ClauseMention, ExternalModelSettings, Extraction, OntoRAGExternal, Relation, build_base, build_route1, build_route2, build_route3, fuse_graphs,
    parse_document, publish_graph, run,
)
from clausegraph.contracts import ClauseTree, Graph, fingerprint
from clausegraph.engine import RunError
from clausegraph.graphs import validate_fused

ONTOLOGY = """
@prefix r2: <https://codex.local/route2/vocab#> .
@prefix ex: <https://example.test/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
ex:equipment rdfs:label "设备"; r2:matchTitleCue "阀门"; ex:has ex:record.
ex:record rdfs:label "记录"; r2:matchHeaderCue "记录"; r2:scoreFloor 0.3.
ex:restricted rdfs:label "限定"; r2:allowedDocumentType "manual"; r2:matchTitleCue "阀门".
"""


def write_docx(path):
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    with ZipFile(path, "w") as document:
        document.writestr("word/styles.xml", f"""
            <w:styles xmlns:w="{namespace}">
              <w:style w:type="paragraph" w:styleId="Base">
                <w:pPr><w:outlineLvl w:val="0"/></w:pPr>
              </w:style>
              <w:style w:type="paragraph" w:styleId="Derived"><w:basedOn w:val="Base"/></w:style>
            </w:styles>
        """)
        document.writestr("word/document.xml", f"""
            <w:document xmlns:w="{namespace}"><w:body>
              <w:p><w:r><w:t>前言</w:t></w:r></w:p>
              <w:p><w:pPr><w:pStyle w:val="Derived"/></w:pPr><w:r><w:t>阀门</w:t></w:r></w:p>
              <w:p><w:pPr><w:outlineLvl w:val="9"/><w:pStyle w:val="Derived"/></w:pPr><w:r><w:t>直属说明</w:t></w:r></w:p>
              <w:p><w:pPr><w:outlineLvl w:val="1"/></w:pPr><w:r><w:t>流量</w:t></w:r></w:p>
              <w:tbl><w:tr><w:tc><w:p><w:r><w:t>记录</w:t></w:r></w:p></w:tc></w:tr>
              <w:tr><w:tc><w:p><w:r><w:t>阀门</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
            </w:body></w:document>
        """)


class Provider:
    configuration = {"provider": "test"}

    async def extract(self, text):
        if "超时" in text:
            raise TimeoutError("test timeout")
        if "缺字段" in text:
            return {"concepts": []}
        if "假引文" in text:
            return Extraction(concepts=[], relations=[
                Relation(subject="假引文", object="假引文", predicate="related", quote="伪造"),
            ])
        concepts = [term for term in ("阀门", "流量") if term in text]
        mentions = []
        for section in text.split("[clause_id=")[1:]:
            clause_id, _, body = section.partition("]\n")
            mentions.extend(ClauseMention(concept=term, clause_id=clause_id) for term in concepts if term in body)
        relations = [Relation(subject="阀门", object="流量", predicate="controls", quote="流量")] if len(concepts) == 2 else []
        return Extraction(concepts=[*concepts, "虚构"], relations=relations, mentions=mentions)


class ParsingTests(unittest.TestCase):
    def test_markdown_structure_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.md"
            path.write_text("前言\n\n# 重复\n\n正文\n\n```\n# 代码\n```\n\n### 子节\n\n|项目|\n|---|\n|记录|\n\n# 重复\n")
            tree = parse_document(path, "notes")
            self.assertEqual(tree.root.blocks[0].text, "前言")
            first, second = tree.root.children
            self.assertEqual(first.title, second.title)
            self.assertNotEqual(first.clause_id, second.clause_id)
            self.assertEqual(first.blocks[1].text, "# 代码")
            self.assertEqual(first.children[0].blocks[0].rows, [["项目"], ["记录"]])
            self.assertEqual(first.children[0].blocks[0].location.start, 13)
            self.assertTrue(tree.diagnostics)
            self.assertEqual(tree, parse_document(path, "notes"))
            path.write_text("没有标题")
            with self.assertRaisesRegex(ValueError, "标题"):
                parse_document(path, "notes")


@unittest.skipUnless(importlib.util.find_spec("ontorag"), "需安装 external 依赖")
class ExternalModelTests(unittest.TestCase):
    def test_mentions_share_the_ontorag_term_request(self):
        import httpx

        requests = []
        client = httpx.AsyncClient
        full_text = "[clause_id=clause:test]\n" + "正文" * 2000
        mention = {"concept": "信号", "clause_id": "clause:test"}

        def respond(request):
            body = json.loads(request.content)
            instruction = body["messages"][0]["content"]
            requests.append(instruction)
            self.assertTrue(any(full_text in message["content"] for message in body["messages"]))
            if '"mentions"' in instruction:
                payload = {"terms": ["信号"], "mentions": [mention]}
            else:
                key = next(key for key in ("typings", "relations", "triples") if f'"{key}"' in instruction)
                payload = {key: []}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

        settings = ExternalModelSettings(model="test-model", base_url="https://example.test/v1",
                                         api_key_env="TEST_CLAUSEGRAPH_KEY")
        with patch.dict(os.environ, {"TEST_CLAUSEGRAPH_KEY": "test-key"}), patch(
            "httpx.AsyncClient", side_effect=lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
        ):
            result = asyncio.run(OntoRAGExternal(settings).extract(full_text))
        self.assertEqual(result.mentions, [ClauseMention(**mention)])
        self.assertEqual(result.concepts, ["信号"])
        self.assertEqual(sum('"mentions"' in instruction for instruction in requests), 1)
        self.assertEqual(len(requests), 4)  # terms, one typing, taxonomy, relations

    def test_authentication_and_sanitized_http_failure(self):
        import httpx

        requests = []
        client = httpx.AsyncClient

        def respond(request):
            requests.append(request)
            return httpx.Response(401, text="do-not-record-response-body")

        def use_transport(**kwargs):
            return client(transport=httpx.MockTransport(respond), **kwargs)

        settings = ExternalModelSettings(model="test-model", base_url="https://example.test/v1",
                                         api_key_env="TEST_CLAUSEGRAPH_KEY")
        with patch.dict(os.environ, {"TEST_CLAUSEGRAPH_KEY": "test-key"}):
            provider = OntoRAGExternal(settings)
        full_text = "阀门" * 2000 + "末尾条款"
        with patch("httpx.AsyncClient", side_effect=use_transport):
            with self.assertRaisesRegex(ValueError, r"HTTP 401") as failure:
                asyncio.run(provider.extract(full_text))
        self.assertNotIn("do-not-record-response-body", str(failure.exception))
        self.assertNotIn("test-key", str(failure.exception))
        self.assertTrue(requests)
        request = requests[0]
        self.assertEqual(str(request.url), "https://example.test/v1/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer test-key")
        body = json.loads(request.content)
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        for request in requests:
            payload = json.loads(request.content)
            self.assertTrue(any(full_text in message["content"] for message in payload["messages"]))
            self.assertNotIn("max_tokens", payload)
            self.assertNotIn("max_completion_tokens", payload)


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.md = self.root / "first.md"
        self.md.write_text("# 阀门\n\n阀门控制流量。\n\n## 流量\n\n流量流量。\n\n# orchid\n")
        self.docx = self.root / "second.docx"
        write_docx(self.docx)
        self.ontology = self.root / "types.ttl"
        self.ontology.write_text(ONTOLOGY)
        self.trees = [parse_document(self.md, "md"), parse_document(self.docx, "word", "manual")]
        self.base = build_base("test", self.trees)

    def routes(self):
        return [build_route1(self.base), build_route2(self.base, self.ontology),
                asyncio.run(build_route3(self.base, Provider()))]

    def manifest(self):
        path = self.root / "manifest.json"
        path.write_text(json.dumps({
            "dataset_id": "test", "ontology": self.ontology.name, "model": {"model": "test"},
            "enable_route3": True,
            "documents": [{"id": tree.document_id, "path": source.name, "type": tree.document_type}
                          for tree, source in zip(self.trees, [self.md, self.docx])],
        }))
        return path

    def test_docx_and_import_validation(self):
        tree = self.trees[1]
        self.assertEqual(tree.root.blocks[0].text, "前言")
        parent = tree.root.children[0]
        self.assertEqual((parent.title, parent.location.start, parent.blocks[0].text), ("阀门", 2, "直属说明"))
        self.assertEqual(parent.children[0].blocks[0].rows, [["记录"], ["阀门"]])
        path = self.root / "tree.json"
        path.write_text(tree.model_dump_json())
        self.assertEqual(parse_document(path, "word"), tree)
        data = tree.model_dump()
        data["root"]["children"][0]["clause_id"] = tree.root.clause_id
        with self.assertRaisesRegex(ValueError, "ID 重复"):
            ClauseTree.model_validate(data)
        with self.assertRaisesRegex(ValueError, "不可重复"):
            build_base("test", [tree, tree])
        old = self.trees[0]
        self.md.write_text("# 阀门\n\n新版本")
        new = parse_document(self.md, "md")
        self.assertNotEqual(old.source_sha256, new.source_sha256)
        self.assertNotEqual(old.root.clause_id, new.root.clause_id)
        self.docx.write_bytes(b"not zip")
        with self.assertRaisesRegex(ValueError, "second.docx"):
            parse_document(self.docx, "word")

    def test_bm25_rank_and_boundary(self):
        graph = build_route1(self.base, top_k=1)
        nodes = {node.id: node for node in self.base.nodes}
        self.assertTrue(graph.edges)
        self.assertEqual(len({edge.source for edge in graph.edges}), len(graph.edges))
        for edge in graph.edges:
            self.assertNotEqual(edge.source, edge.target)
            self.assertNotIn("orchid", [nodes[key].label for key in (edge.source, edge.target)])
            self.assertLess(edge.properties["bm25"], 0)
            self.assertEqual(edge.properties["rank"], 1)
        all_edges = build_route1(self.base).edges
        pairs = [(nodes[edge.source].properties["document_id"], nodes[edge.target].properties["document_id"])
                 for edge in all_edges]
        self.assertTrue(any(left == right for left, right in pairs))
        self.assertTrue(any(left != right for left, right in pairs))
        reordered = build_base("test", self.trees[::-1])
        self.assertEqual(graph.normalized(), build_route1(reordered, top_k=1).normalized())
        tie_files = []
        for i, body in enumerate(["# Needle", "# Needle", "# Needle"]):
            path = self.root / f"tie-{i}.md"
            path.write_text(body)
            tie_files.append(parse_document(path, f"tie-{i}"))
        tied = build_route1(build_base("tie", tie_files), top_k=1)
        ordered = sorted(edge.source for edge in tied.edges)
        self.assertEqual({edge.source: edge.target for edge in tied.edges},
                         {source: next(key for key in ordered if key != source) for source in ordered})

    def test_route3_processes_one_full_text_per_document(self):
        self.md.write_text("# 首部\n\n" + "正文" * 2000 + "\n\n# 尾部\n\n末尾概念")
        base = build_base("fulltext", [parse_document(self.md, "md")])
        calls = []
        ids = {node.label: node.id for node in base.nodes if node.kind == "Clause"}
        first, last = ids["首部"], ids["尾部"]

        class RecordingProvider:
            configuration = {"provider": "test"}

            async def extract(self, text):
                calls.append(text)
                implicit = ClauseMention(concept="隐含概念", clause_id=last)
                return Extraction(concepts=["首部", "末尾概念", "隐含概念"], relations=[
                    Relation(subject="首部", predicate="relates_to", object="隐含概念"),
                ], mentions=[ClauseMention(concept="首部", clause_id=first), implicit, implicit,
                             ClauseMention(concept="末尾概念", clause_id="invalid")])

        graph = asyncio.run(build_route3(base, RecordingProvider()))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], f"[clause_id={first}]\n首部\n" + "正文" * 2000 +
                         f"\n[clause_id={last}]\n尾部\n末尾概念")
        self.assertEqual({node.label for node in graph.nodes}, {"首部", "隐含概念"})
        attachments = [edge for edge in graph.edges if edge.kind == "MENTIONS"]
        self.assertEqual(len(attachments), 2)
        self.assertEqual(next(edge for edge in attachments if edge.source == last).properties["review_status"], "pending")
        self.assertEqual(graph.metadata["unresolved_mentions"][0]["reason"], "unknown_clause_id")
        relation = next(edge for edge in graph.edges if edge.kind == "CONTENT_RELATION")
        self.assertEqual({item.excerpt for item in relation.evidence}, {"首部", "尾部"})
        self.assertEqual(len({item.clause_id for item in relation.evidence}), 2)

    def test_bm25_without_cutoff_keeps_every_match(self):
        self.md.write_text("# Shared\n\n" * 15 + "# Orchid\n")
        base = build_base("unlimited", [parse_document(self.md, "md")])
        edges = build_route1(base, top_k=None).edges
        self.assertEqual(len(edges), 15 * 14)
        source = edges[0].source
        self.assertEqual([edge.properties["rank"] for edge in edges if edge.source == source],
                         list(range(1, 15)))

    def test_rdf_optional_type_threshold_and_rows(self):
        graph = build_route2(self.base, self.ontology)
        related = [edge for edge in graph.edges if edge.kind == "TYPE_RELATION"]
        self.assertEqual(len(related), 1)
        attachment = [edge for edge in graph.edges if edge.kind == "CLASSIFIED_AS"]
        self.assertTrue(any(item.row == 1 and item.excerpt == "记录" for edge in attachment for item in edge.evidence))
        nodes = {node.id: node for node in self.base.nodes}
        restricted = [edge for edge in attachment if edge.target.endswith("/restricted")]
        self.assertEqual([nodes[edge.source].properties["document_id"] for edge in restricted], ["word"])
        self.assertTrue(graph.metadata["unmatched_clauses"])
        self.ontology.write_text(ONTOLOGY.replace('"manual"', '"new-type"'))
        changed = build_route2(self.base, self.ontology)
        self.assertNotEqual(graph.config_fingerprint, changed.config_fingerprint)
        self.assertFalse(any(edge.target.endswith("/restricted") for edge in changed.edges))
        self.ontology.write_text(ONTOLOGY + 'ex:restricted r2:matchIdentifierPattern "[".')
        with self.assertRaisesRegex(ValueError, "编号规则"):
            build_route2(self.base, self.ontology)

    def test_extraction_and_fusion(self):
        routes = self.routes()
        third = routes[-1]
        self.assertEqual(third.status, "ready")
        self.assertEqual(len([node for node in third.nodes if node.label == "阀门"]), 2)
        self.assertNotIn("虚构", [node.label for node in third.nodes])
        relations = [edge for edge in third.edges if edge.kind == "CONTENT_RELATION"]
        self.assertEqual(relations[0].properties["review_status"], "pending")
        graph = fuse_graphs(self.base, routes)
        self.assertEqual(graph.normalized(), fuse_graphs(self.base, routes[::-1]).normalized())
        self.assertEqual(len(graph.nodes), len(self.base.nodes) + len(routes[1].nodes) + len(third.nodes))
        validate_fused(Graph.model_validate_json(graph.model_dump_json()))
        broken = routes[0].model_copy(deep=True)
        broken.edges[0].evidence[0].excerpt = "无效证据"
        with self.assertRaisesRegex(ValueError, "无法回查"):
            fuse_graphs(self.base, [broken, *routes[1:]])
        broken.edges[0].target = "missing"
        with self.assertRaisesRegex(ValueError, "端点"):
            fuse_graphs(self.base, [broken, *routes[1:]])
        other = build_base("different", self.trees)
        with self.assertRaisesRegex(ValueError, "底图不一致"):
            fuse_graphs(other, routes)

    def test_full_run_and_strict_failure(self):
        path = self.manifest()
        output = self.root / "ok"
        result = asyncio.run(run(path, output, provider=Provider()))
        self.assertEqual(result["status"], "ready")
        self.assertTrue((output / "fused.json").is_file())
        with self.assertRaisesRegex(ValueError, "输出目录必须为空"):
            asyncio.run(run(path, output, provider=Provider()))
        self.md.write_text("# 阀门\n\n超时\n\n假引文\n\n缺字段")
        output = self.root / "broken"
        with patch("clausegraph.engine.publish_graph") as publish:
            with self.assertRaises(RunError):
                asyncio.run(run(path, output, provider=Provider(), neo4j_uri="bolt://unused"))
            publish.assert_not_called()
        report = json.loads((output / "report.json").read_text())
        self.assertEqual(report["failure"]["stage"], "route3")
        self.assertEqual(len(report["graphs"]["route3"]["failed_units"]), 1)
        graph = Graph.model_validate_json((output / "fused.json").read_bytes())
        with self.assertRaisesRegex(ValueError, "已开启路线均完成"):
            publish_graph(graph, "bolt://unused")

    def test_route3_default_off_and_explicit_override(self):
        path = self.manifest()
        data = json.loads(path.read_text())
        data.pop("enable_route3")
        data.pop("model")
        path.write_text(json.dumps(data))
        with patch("clausegraph.engine.OntoRAGExternal") as external:
            result = asyncio.run(run(path, self.root / "off"))
            external.assert_not_called()
        self.assertEqual(result["graphs"]["route3"]["status"], "skipped")
        graph = Graph.model_validate_json((self.root / "off" / "fused.json").read_bytes())
        validate_fused(graph)
        self.assertFalse(any(node.kind == "ContentConcept" for node in graph.nodes))
        result = asyncio.run(run(path, self.root / "on", provider=Provider(), enable_route3=True))
        self.assertEqual(result["graphs"]["route3"]["status"], "ready")

    def test_cli_independent_stages(self):
        commands = [
            ["parse", str(self.md), "--id", "md", "--output", "tree.json"],
            ["base", "tree.json", "--dataset", "test", "--output", "base.json"],
            ["build", "--route", "route1", "--base", "base.json", "--output", "route1.json"],
            ["validate", "graph", "route1.json", "--base", "base.json"],
            ["schema", "graph"],
        ]
        for command in commands:
            result = subprocess.run([sys.executable, "-m", "clausegraph", *command], cwd=self.root,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.getenv("CLAUSEGRAPH_TEST_NEO4J"), "需提供真实 Neo4j")
    def test_neo4j_concurrent_publish_and_isolation(self):
        from neo4j import GraphDatabase

        uri = os.environ["CLAUSEGRAPH_TEST_NEO4J"]
        dataset = "test-" + uuid4().hex
        self.base = build_base(dataset, self.trees)
        graph = fuse_graphs(self.base, self.routes())
        with GraphDatabase.driver(uri) as driver:
            try:
                # Use the real transaction and suppress a write to trigger the count guard.
                wrapper = MagicMock()
                wrapper.__enter__.return_value = wrapper
                wrapped_session = wrapper.session.return_value.__enter__.return_value

                def execute_with_missing_edge(write):
                    with driver.session() as session:
                        def inject(tx):
                            transaction = MagicMock()
                            transaction.run.side_effect = lambda query, **params: (
                                MagicMock() if "MERGE (a)-[r:SIMILAR_TO" in query else tx.run(query, **params)
                            )
                            return write(transaction)
                        return session.execute_write(inject)

                with driver.session() as session:
                    wrapped_session.run.side_effect = session.run
                    wrapped_session.execute_write.side_effect = execute_with_missing_edge
                    with patch.object(GraphDatabase, "driver", return_value=wrapper):
                        with self.assertRaisesRegex(RuntimeError, "计数"):
                            publish_graph(graph, uri)
                    count = session.run("MATCH (s:ClauseGraphSnapshot {dataset_id:$id}) RETURN count(s) AS n",
                                        id=dataset).single()["n"]
                    self.assertEqual(count, 0)
                with ThreadPoolExecutor(3) as pool:
                    results = list(pool.map(lambda _: publish_graph(graph, uri), range(3)))
                self.assertEqual(results, [results[0]] * 3)
                snapshot = results[0]["snapshot_id"]
                self.assertEqual(results[0]["edges"], len(graph.edges))
                self.md.write_text("# 阀门\n\n阀门控制流量。\n\n# 新版本")
                self.base = build_base(dataset, [parse_document(self.md, "md"), self.trees[1]])
                next_graph = fuse_graphs(self.base, self.routes())
                next_snapshot = publish_graph(next_graph, uri)["snapshot_id"]
                self.assertNotEqual(snapshot, next_snapshot)
                with driver.session() as session:
                    records = session.run("""
                        MATCH (s:ClauseGraphSnapshot {id:$id})-[:HAS_NODE]->(a:Clause)
                              -[r:MENTIONS]->(:ContentConcept {label:"阀门"})
                        MATCH (a)-[:IN_DOCUMENT]->(:Document)-[:REPRESENTS_VERSION]->(v:ClauseGraphDocumentVersion)
                        RETURN s.storage_schema_version AS schema, s.graph_fingerprint AS fingerprint,
                               properties(a) AS clause, r.assertions_json AS assertions,
                               r.evidence_json AS legacy, v.source_index_json AS source_index,
                               [(a)-[:REPRESENTS_CLAUSE]->(:VersionClause)-[:HAS_BLOCK]->(p:Paragraph) | p{.unit,.start,.end,.text}] AS sources
                        """, id=snapshot).data()
                    self.assertTrue(records)
                    excerpts = []
                    for row in records:
                        self.assertEqual(row["schema"], 2)
                        self.assertEqual(row["fingerprint"], fingerprint(graph.normalized()))
                        self.assertEqual(row["clause"]["kind"], "Clause")
                        self.assertNotIn("properties_json", row["clause"])
                        self.assertNotIn("document_id", row["clause"])
                        self.assertIsNone(row["legacy"])
                        self.assertIsNone(row["source_index"])
                        for assertion in json.loads(row["assertions"]):
                            for ref in assertion["evidence_refs"]:
                                if ref["start"] == row["clause"].get("location_start"):
                                    source = row["clause"]["label"]
                                else:
                                    source = next(part["text"] for part in row["sources"]
                                                  if all(part.get(key) == ref.get(key)
                                                         for key in ("unit", "start", "end", "row")))
                                excerpts.append(source[ref["from"]:ref["to"]])
                    self.assertTrue(any("阀门" in excerpt for excerpt in excerpts))
                    count = session.run("MATCH (s:ClauseGraphSnapshot {dataset_id:$id}) RETURN count(s) AS n",
                                        id=dataset).single()["n"]
                    self.assertEqual(count, 2)
            finally:
                with driver.session() as session:
                    version_ids = session.run("""
                        MATCH (:ClauseGraphSnapshot {dataset_id:$id})-[:USES_DOCUMENT]->(v)
                        RETURN collect(DISTINCT v.id) AS versions
                        """, id=dataset).single(strict=True)["versions"]
                    session.run("""
                        MATCH (s:ClauseGraphSnapshot {dataset_id:$id})-[:HAS_NODE]->(n:ClauseGraphNode)
                        DETACH DELETE n
                        """, id=dataset).consume()
                    session.run("MATCH (s:ClauseGraphSnapshot {dataset_id:$id}) DETACH DELETE s",
                                id=dataset).consume()
                    session.run("""
                        MATCH (v:ClauseGraphDocumentVersion {origin:'standalone'})
                        WHERE v.id IN $ids AND NOT EXISTS { MATCH (:ClauseGraphSnapshot)-[:USES_DOCUMENT]->(v) }
                        OPTIONAL MATCH (c:VersionClause {version_id:v.id})
                        OPTIONAL MATCH (c)-[:HAS_BLOCK]->(b)
                        OPTIONAL MATCH (b)-[:HAS_ROW]->(r)
                        DETACH DELETE r, b, c, v
                    """, ids=version_ids).consume()

    @unittest.skipUnless(os.getenv("CLAUSEGRAPH_TEST_NEO4J"), "需提供真实 Neo4j")
    def test_project_files_and_snapshots_are_independent(self):
        from neo4j import GraphDatabase

        from clausegraph import create_snapshot, import_document, init_project, list_project_files

        uri = os.environ["CLAUSEGRAPH_TEST_NEO4J"]
        project = "test-" + uuid4().hex
        connection = {"neo4j_uri": uri}
        try:
            init_project(project, self.ontology, **connection)
            first = asyncio.run(import_document(project, self.md, "md", **connection))
            second = asyncio.run(import_document(project, self.docx, "word", document_type="manual",
                                                 enable_route3=True, provider=Provider(), **connection))
            with GraphDatabase.driver(uri) as driver, driver.session() as session:
                count = session.run("MATCH (s:ClauseGraphSnapshot {dataset_id:$id}) RETURN count(s) AS n",
                                    id=project).single()["n"]
                self.assertEqual(count, 0)
            version_ids = [first["version_id"], second["version_id"]]
            with patch("clausegraph.projects._build_route2", side_effect=AssertionError("unexpected route2")), \
                 patch("clausegraph.projects.build_route3", side_effect=AssertionError("unexpected route3")):
                single = create_snapshot(project, version_ids=version_ids[:1], **connection)
                full = create_snapshot(project, all_active=True, **connection)
                again = create_snapshot(project, version_ids=version_ids[::-1], **connection)
                capped = create_snapshot(project, all_active=True, top_k=1, **connection)
            self.assertEqual(full, again)
            self.assertNotEqual(single["snapshot_id"], full["snapshot_id"])
            self.assertNotEqual(capped["snapshot_id"], full["snapshot_id"])
            self.assertEqual(capped["nodes"], full["nodes"])
            with GraphDatabase.driver(uri) as driver, driver.session() as session:
                counts = session.run("""
                    MATCH (s:ClauseGraphSnapshot)-[:HAS_NODE]->(a)-[r]->(b:ClauseGraphNode)
                    WHERE s.id IN $ids AND b.snapshot_id = s.id AND type(r) <> "IN_DOCUMENT"
                    RETURN s.id AS id, type(r) AS kind, count(r) AS count, max(r.rank) AS rank
                """, ids=[full["snapshot_id"], capped["snapshot_id"]]).data()
                shared = session.run("""
                    MATCH (s:ClauseGraphSnapshot)-[:HAS_NODE]->(c:Clause)
                          -[:REPRESENTS_CLAUSE]->(:VersionClause)-[:HAS_BLOCK]->(t:Table)
                    WHERE s.id IN $ids AND NOT EXISTS { MATCH (c)-[:HAS_BLOCK]->() }
                    MATCH (t)-[:HAS_ROW]->(r:TableRow)
                    WITH t, r, count(DISTINCT s) AS snapshots
                    WHERE snapshots = 2 AND size(r.cells) > 0
                    RETURN count(r) AS shared_rows
                """, ids=[full["snapshot_id"], capped["snapshot_id"]]).single(strict=True)
                self.assertGreater(shared["shared_rows"], 0)
            if shutil.which("node"):
                js = Path(__file__).resolve().parents[1] / "src/clausegraph/web/workbench.js"
                queries = json.loads(subprocess.check_output(["node", "-e", """
                    const fs = require("node:fs");
                    const vm = require("node:vm");
                    const source = fs.readFileSync(process.argv[1], "utf8");
                    const start = source.indexOf("function queryFor(");
                    const end = source.indexOf("function renderQuery(", start);
                    const queryFor = vm.runInNewContext(source.slice(start, end) + "; queryFor");
                    console.log(JSON.stringify(Object.fromEntries(
                        ["tree", "route1", "route2", "route3"].map(kind =>
                            [kind, queryFor(process.argv[2], kind, 1)]))));
                """, str(js), full["snapshot_id"]], text=True))
                expected = {
                    "tree": {"CONTAINS", "HAS_BLOCK", "HAS_ROW"},
                    "route1": {"SIMILAR_TO"},
                    "route2": {"CLASSIFIED_AS", "TYPE_RELATION"},
                    "route3": {"MENTIONS", "CONTENT_RELATION"},
                }
                with GraphDatabase.driver(uri) as driver, driver.session() as session:
                    for kind, query in queries.items():
                        records = list(session.run(query))
                        actual = {value.type for row in records for value in row.values()
                                  if hasattr(value, "type")}
                        self.assertEqual(actual, expected[kind], kind)
                        if kind == "route1":
                            self.assertTrue(all(row["r"]["rank"] == 1 for row in records))
            by_snapshot = {item["snapshot_id"]: {} for item in (full, capped)}
            for row in counts:
                by_snapshot[row["id"]][row["kind"]] = (row["count"], row["rank"])
            full_counts = by_snapshot[full["snapshot_id"]]
            capped_counts = by_snapshot[capped["snapshot_id"]]
            self.assertEqual(capped_counts.pop("SIMILAR_TO")[1], 1)
            self.assertGreater(full_counts.pop("SIMILAR_TO")[1], 1)
            self.assertEqual(capped_counts, full_counts)
            self.assertIn("CLASSIFIED_AS", full_counts)
            self.assertIn("MENTIONS", full_counts)
            self.assertNotIn("ATTACHED_TO", full_counts)
            self.md.write_text("# 阀门\n\n新版本")
            with self.assertRaisesRegex(ValueError, "--replace"):
                asyncio.run(import_document(project, self.md, "md", **connection))
            replacement = asyncio.run(import_document(project, self.md, "md", replace=True, **connection))
            self.assertEqual(replacement["replaced_version_id"], first["version_id"])
            history = {item["id"]: item["active"] for item in list_project_files(project, history=True, **connection)}
            self.assertFalse(history[first["version_id"]])
            with self.assertRaisesRegex(ValueError, "活动文件版本"):
                create_snapshot(project, version_ids=version_ids, **connection)
            with GraphDatabase.driver(uri) as driver, driver.session() as session:
                count = session.run("""
                    MATCH (:ClauseGraphSnapshot {id:$snapshot})-[:USES_DOCUMENT]->(d)
                    WHERE d.origin = "project" AND d.parser_version IS NOT NULL AND d.tree_json IS NULL
                      AND EXISTS { MATCH (d)-[:HAS_ROOT]->(:VersionClause) }
                    RETURN count(d) AS n
                """, snapshot=full["snapshot_id"]).single()["n"]
                self.assertEqual(count, 2)
        finally:
            with GraphDatabase.driver(uri) as driver, driver.session() as session:
                session.run("""
                    MATCH (s:ClauseGraphSnapshot {dataset_id:$project})-[:HAS_NODE]->(n)
                    DETACH DELETE n
                """, project=project).consume()
                session.run("""
                    MATCH (:ClauseGraphProject {id:$project})-[:HAS_VERSION]->(d:ClauseGraphDocumentVersion)
                    OPTIONAL MATCH (c:VersionClause {version_id:d.id})
                    OPTIONAL MATCH (c)-[:HAS_BLOCK]->(b)
                    OPTIONAL MATCH (b)-[:HAS_ROW]->(r)
                    DETACH DELETE r, b, c, d
                """, project=project).consume()
                for label, key in (("ClauseGraphSnapshot", "dataset_id"), ("ClauseGraphProject", "id")):
                    session.run(f"MATCH (n:{label} {{{key}:$project}}) DETACH DELETE n",
                                project=project).consume()
