"""Directed nearest-neighbor retrieval over the entire clause collection."""

import re
import sqlite3
import unicodedata
from contextlib import closing

from .contracts import Edge, Evidence, Location
from .graphs import route_graph

TOKEN = re.compile(r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+")
TOKENIZER_VERSION = "nfkc-casefold-han-bigram-v1"


def tokenize(text):
    words = TOKEN.findall(unicodedata.normalize("NFKC", text).casefold())
    for word in words:
        if "\u3400" <= word[0] <= "\u9fff" and len(word) > 1:
            yield from (word[index:index + 2] for index in range(len(word) - 1))
        else:
            yield word


def build_route1(base, *, top_k=None):
    if top_k is not None and (type(top_k) is not int or top_k < 1):
        raise ValueError("top_k 必须为正整数或 None")
    by_id = {}
    for node in base.nodes:
        if node.kind == "Clause":
            parts = [(item, set(tokenize(item["text"]))) for item in node.properties["sources"]]
            terms = [word for item, _ in parts for word in tokenize(item["text"])]
            if terms:
                by_id[node.id] = (parts, terms)
    edges = []
    limit = -1 if top_k is None else min(top_k, len(by_id))
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE VIRTUAL TABLE clauses USING fts5(id UNINDEXED, content, tokenize='unicode61 remove_diacritics 0')")
        db.executemany("INSERT INTO clauses VALUES (?, ?)",
                       ((key, " ".join(by_id[key][1])) for key in sorted(by_id)))
        for source in sorted(by_id):
            parts, terms = by_id[source]
            query = " OR ".join(f'"{term}"' for term in sorted(set(terms)))
            hits = db.execute(
                "SELECT id, bm25(clauses) AS score FROM clauses WHERE clauses MATCH ? AND id != ? "
                "ORDER BY score, id LIMIT ?", (query, source, limit)
            )
            rank = 0
            for target, score in hits:
                target_parts, target_terms = by_id[target]
                matched = set(terms).intersection(target_terms)
                if not matched:
                    continue
                rank += 1
                evidence = []
                for key, side in ((source, parts), (target, target_parts)):
                    item, _ = max(side, key=lambda pair: len(pair[1] & matched))
                    evidence.append(Evidence(clause_id=key, location=Location.model_validate(item["location"]),
                                             row=item["row"], excerpt=item["text"]))
                edges.append(Edge(source=source, target=target, kind="SIMILAR_TO", predicate="bm25",
                                  evidence=evidence, properties={"bm25": score, "rank": rank,
                                                                 "terms": sorted(matched)}))
    return route_graph(base, "route1", {"top_k": top_k, "tokenizer": TOKENIZER_VERSION,
                                       "sqlite": sqlite3.sqlite_version}, edges=edges)
