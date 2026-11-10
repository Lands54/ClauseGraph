"""Measure deterministic stages in a fresh process for each corpus size."""

import argparse
import asyncio
import json
import resource
import sys
import tempfile
from pathlib import Path
from time import perf_counter

from clausegraph import Extraction, build_base, build_route1, build_route2, build_route3, fuse_graphs, parse_document


class EmptyProvider:
    configuration = {"provider": "benchmark-stub"}

    async def extract(self, text):
        return Extraction(concepts=[], relations=[])


def benchmark(size):
    timings = {}

    def measure(stage, build):
        start = perf_counter()
        result = build()
        timings[stage] = round(perf_counter() - start, 3)
        return result

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        files = []
        for index in range(10):
            path = root / f"{index}.md"
            path.write_text("".join(
                f"# sector{n % 100}\n\nrecord{n}\n\n" for n in range(index, size, 10)
            ), encoding="utf-8")
            files.append(path)
        ontology = root / "types.ttl"
        ontology.write_text("""
        @prefix r2: <https://codex.local/route2/vocab#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        <urn:record> rdfs:label "Record"; r2:matchBodyCue "record"; r2:scoreFloor 0.2 .
        """, encoding="utf-8")
        trees = measure("parse", lambda: [parse_document(path, path.stem) for path in files])
        base = measure("base", lambda: build_base("benchmark", trees))
        first = measure("route1", lambda: build_route1(base, top_k=12))
        second = measure("route2", lambda: build_route2(base, ontology))
        third = measure("route3_stub", lambda: asyncio.run(build_route3(base, EmptyProvider())))
        fused = measure("fuse", lambda: fuse_graphs(base, [first, second, third]))
        assert len(first.edges) <= size * 12
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {"clauses": size, "seconds": timings, "peak_mib": round(rss / (1024**2 if sys.platform == "darwin" else 1024), 1),
                "bm25_edges": len(first.edges), "total_nodes": len(fused.nodes), "total_edges": len(fused.edges)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("size", type=int, choices=(1000, 10000))
    print(json.dumps(benchmark(parser.parse_args().size), ensure_ascii=False))
