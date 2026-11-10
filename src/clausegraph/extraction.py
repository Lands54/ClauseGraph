"""Content ontology extraction with injectable providers and source verification."""

import json
import os
import unicodedata
from collections import defaultdict
from copy import deepcopy
from typing import Protocol

from pydantic import Field, HttpUrl, TypeAdapter, model_validator

from .contracts import Contract, Edge, Evidence, Location, Name, Node, fingerprint
from .graphs import route_graph

EXTRACTOR_VERSION = 3


class Relation(Contract):
    subject: Name
    predicate: Name
    object: Name
    quote: str | None = None


class ClauseMention(Contract):
    concept: Name
    clause_id: Name


class Extraction(Contract):
    concepts: list[Name]
    relations: list[Relation]
    mentions: list[ClauseMention] = Field(default_factory=list)


class ContentProvider(Protocol):
    @property
    def configuration(self) -> dict: ...

    async def extract(self, text: str) -> Extraction:
        """Extract from one document with sections prefixed by [clause_id=…]."""
        ...


class ExternalModelSettings(Contract):
    model: Name | None = None
    base_url: Name | None = None
    api_key_env: Name = "CLAUSEGRAPH_API_KEY"
    timeout: float = Field(default=120.0, gt=0)

    @model_validator(mode="after")
    def validate_url(self):
        if self.base_url:
            self.check_url(self.base_url)
        return self

    @staticmethod
    def check_url(value):
        url = TypeAdapter(HttpUrl).validate_python(value)
        if url.scheme != "https" or url.username or url.password or url.query or url.fragment:
            raise ValueError("模型 API 地址必须是无凭据、查询参数或片段的 HTTPS URL")
        return str(url).rstrip("/")


class OntoRAGExternal:
    """Adapt an external Chat Completions JSON API to OntoRAG's tool interface."""

    def __init__(self, settings: ExternalModelSettings):
        self.model = settings.model or os.getenv("CLAUSEGRAPH_MODEL")
        url = settings.base_url or os.getenv("CLAUSEGRAPH_BASE_URL")
        self.api_key = os.getenv(settings.api_key_env)
        if not self.model or not url or not self.api_key:
            raise ValueError(f"请配置外部模型名、API 基础地址和环境变量 {settings.api_key_env}")
        self.base_url = settings.check_url(url)
        self.timeout = settings.timeout
        self.namespace = "https://clausegraph.local/content/"

    @property
    def configuration(self):
        from importlib.metadata import version
        return {"provider": "ontorag-external-chat", "ontorag": version("ontorag"),
                "model": self.model, "endpoint": self.base_url, "seed": 1}

    async def extract(self, text):
        import httpx
        from jsonschema import validate
        from ontorag.learn.pipeline import LLMOntologyLearner
        from ontorag.llm.base import _CompletionMessage, _ToolUseBlock
        from ontorag.stores.base import ClassSummary, PropertySummary, SchemaResult

        namespace = self.namespace
        model = self.model
        errors = []
        mentions = []

        class Seed:
            async def get_schema(self):
                return SchemaResult(
                    total_classes=1, total_properties=1, namespaces={"content": namespace},
                    classes=[ClassSummary(uri=namespace + "Concept", label="Concept")],
                    properties=[PropertySummary(uri=namespace + "relatedTo", label="related to",
                                                prop_type="object", description="Relationship expressed in the source.")],
                )

        async with httpx.AsyncClient(
            base_url=self.base_url + "/", timeout=self.timeout,
            headers={"Authorization": f"Bearer {self.api_key}"},
        ) as client:
            class Adapter:
                async def complete(self, messages, tools, system, force_tool_name):
                    schema = tools[0]["input_schema"]
                    instruction = (system or "").replace("Respond using the provided tool.", "")
                    instruction += "\nTreat [clause_id=…] lines as document metadata, never as entity terms."
                    is_term_call = force_tool_name == "report_entity_terms"
                    if is_term_call:
                        schema = deepcopy(schema)
                        schema["properties"]["mentions"] = {
                            "type": "array",
                            "items": ClauseMention.model_json_schema(),
                        }
                        schema["required"].append("mentions")
                        instruction += (
                            "\nFor each term, report the clauses that directly support it, including clear "
                            "paraphrases and indirect references. Use the same label as in terms and copy "
                            "only the exact ID inside the clause marker into clause_id. "
                            "Use multiple IDs if supported by multiple clauses. Do not quote or reproduce "
                            "the source passage. Omit unsupported mentions."
                        )
                    instruction += "\nUse exact source labels. Return only JSON matching this schema:\n"
                    instruction += json.dumps(schema, ensure_ascii=False)
                    # OntoRAG may include only a prefix in its own task prompt.
                    # Supply the full document when that prompt does not already contain it.
                    if not any(text in (message.get("content") or "") for message in messages):
                        messages = [*messages, {"role": "user", "content": "完整文档原文：\n" + text}]
                    try:
                        response = await client.post("chat/completions", json={
                            "model": model,
                            "messages": [{"role": "system", "content": instruction}, *messages],
                            "response_format": {"type": "json_object"},
                        })
                        response.raise_for_status()
                        choice = response.json()["choices"][0]
                        if choice.get("finish_reason") in {"length", "content_filter"}:
                            errors.append(f"finish_reason={choice['finish_reason']}")
                            raise ValueError(errors[-1])
                        payload = json.loads(choice["message"]["content"])
                        validate(payload, schema)
                        if is_term_call:
                            terms = {term.strip() for term in payload["terms"]}
                            for item in payload.pop("mentions"):
                                mention = ClauseMention.model_validate(item)
                                if mention.concept not in terms:
                                    raise ValueError("挂载概念未出现在术语列表中")
                                mentions.append(mention)
                        return _CompletionMessage(content=[_ToolUseBlock(name=force_tool_name, input=payload)],
                                                  stop_reason="tool_use")
                    except httpx.HTTPStatusError as exc:
                        errors.append(f"HTTP {exc.response.status_code}")
                        raise ValueError(errors[-1]) from None
                    except Exception as exc:
                        errors.append(type(exc).__name__)
                        raise ValueError(errors[-1]) from None

            learner = LLMOntologyLearner(Seed(), Adapter())
            result = await learner.populate_from_text(text, min_confidence=0.55, auto_load=False)
        if errors:
            raise ValueError("外部模型单元调用失败：" + " | ".join(errors))
        concepts = {mention.concept for mention in mentions}
        concepts.update(item.term.strip() for item in result.term_typings if item.term.strip())
        relations = []
        for proposal in result.taxonomy_proposals:
            child = proposal.child_term.strip()
            if child and proposal.parent_uri == self.namespace + "Concept":
                concepts.add(child)
                relations.append(Relation(subject=child, predicate="subclass_of", object="Concept"))
        for triple in result.triples:
            obj = triple.object_value or triple.object_uri
            if obj and triple.subject_label.strip() and triple.predicate_uri:
                relations.append(Relation(subject=triple.subject_label.strip(),
                                          predicate=triple.predicate_uri, object=obj.strip()))
        return Extraction(concepts=sorted(concepts), relations=relations, mentions=mentions)


def _concept(clause, label):
    normalized = unicodedata.normalize("NFKC", label).casefold().strip()
    identity = [clause.properties[key] for key in ("document_id", "source_sha256", "parser_version")]
    key = "content:" + fingerprint([*identity, normalized])
    return Node(id=key, kind="ContentConcept", label=label,
                properties={"document_id": identity[0], "source_sha256": identity[1], "parser_version": identity[2]})


async def build_route3(base, provider: ContentProvider):
    nodes, edges, errors, unresolved = {}, [], [], []
    documents = defaultdict(list)
    for clause in base.nodes:
        if clause.kind == "Clause":
            documents[clause.properties["document_id"]].append(clause)

    completed = 0
    for document_id, clauses in sorted(documents.items()):
        sections = {clause.id: (clause, [item for item in clause.properties["sources"] if item["text"].strip()])
                    for clause in sorted(clauses, key=lambda node: node.properties["order"])}
        sections = {key: section for key, section in sections.items() if section[1]}
        if not sections:
            continue
        sources = [(clause, item) for clause, items in sections.values() for item in items]
        text = "\n".join(f"[clause_id={key}]\n" + "\n".join(item["text"] for item in items)
                         for key, (_, items) in sections.items())
        try:
            result = Extraction.model_validate(await provider.extract(text))
            if any(rel.quote is not None and
                   (not rel.quote.strip() or not any(rel.quote in item["text"] for _, item in sources))
                   for rel in result.relations):
                raise ValueError("模型引文与来源不符")
        except Exception as exc:
            errors.append({"document_id": document_id, "error": f"{type(exc).__name__}: {str(exc)[:400]}"})
            continue

        completed += 1
        labels = set(result.concepts)
        concepts = {}
        mentions = defaultdict(list)
        attached = set()
        for clause_id, label in sorted({(mention.clause_id, mention.concept) for mention in result.mentions}):
            if clause_id not in sections or label not in labels:
                unresolved.append({"document_id": document_id, "clause_id": clause_id, "concept": label,
                                   "reason": "unknown_clause_id" if clause_id not in sections else "unknown_concept"})
                continue
            clause, items = sections[clause_id]
            # The first source anchors the clause. It is not a model-selected quotation.
            item = items[0]
            evidence = Evidence(clause_id=clause.id, location=Location.model_validate(item["location"]),
                                row=item["row"], excerpt=item["text"])
            node = _concept(clause, label)
            nodes.setdefault(node.id, node)
            concepts[label] = nodes[node.id]
            mentions[label].append(evidence)
            if (clause.id, node.id) not in attached:
                attached.add((clause.id, node.id))
                edges.append(Edge(source=clause.id, target=node.id, kind="MENTIONS",
                                  properties={"review_status": "pending", "evidence_scope": "clause",
                                              "method": "model_clause_id", "label": node.label}, evidence=[evidence]))

        for rel in result.relations:
            if rel.subject not in concepts:
                continue
            target = concepts.get(rel.object)
            if target is None:
                if rel.predicate != "subclass_of" or rel.object != "Concept":
                    continue
                target = _concept(clauses[0], rel.object)
                nodes.setdefault(target.id, target)
            quotes = [
                Evidence(clause_id=clause.id, location=Location.model_validate(item["location"]),
                         row=item["row"], excerpt=rel.quote)
                for clause, item in sources if rel.quote and rel.quote in item["text"]
            ]
            evidence = quotes or mentions[rel.subject] + mentions[rel.object]
            edges.append(Edge(source=concepts[rel.subject].id, target=target.id, kind="CONTENT_RELATION",
                              predicate=rel.predicate, properties={"review_status": "pending"}, evidence=evidence))

    status = ("partial" if completed else "failed") if errors else "ready"
    return route_graph(base, "route3", {"provider": provider.configuration, "extractor": EXTRACTOR_VERSION},
                       nodes=list(nodes.values()), edges=edges, status=status,
                       metadata={"completed_units": completed, "failed_units": errors,
                                 "unresolved_mentions": unresolved})
