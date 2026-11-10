"""Command line interface for independent stages and complete runs."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .bm25 import build_route1
from .contracts import ClauseTree, Graph
from .engine import Manifest, run, write_json
from .extraction import ExternalModelSettings, OntoRAGExternal, build_route3
from .graphs import ROUTES, _evidence_index, _validate_route, build_base, fuse_graphs, validate_base, validate_fused
from .ontology import build_route2, load_ontology
from .parsing import parse_document
from .publishing import publish_graph
from .projects import create_snapshot, import_document, init_project, list_project_files


def _read(model, path):
    return model.model_validate_json(Path(path).read_bytes())


def _parser():
    parser = argparse.ArgumentParser(prog="clausegraph", description="通用条款图引擎")
    sub = parser.add_subparsers(dest="command", required=True)

    parse = sub.add_parser("parse")
    parse.add_argument("document")
    parse.add_argument("--id", required=True)
    parse.add_argument("--type")
    parse.add_argument("--output", required=True)

    base = sub.add_parser("base")
    base.add_argument("trees", nargs="+")
    base.add_argument("--dataset", required=True)
    base.add_argument("--output", required=True)

    build = sub.add_parser("build")
    build.add_argument("--base", required=True)
    build.add_argument("--route", required=True, choices=ROUTES)
    build.add_argument("--output", required=True)
    build.add_argument("--top-k", type=int, help="可选候选上限，默认保留全部命中")
    build.add_argument("--ontology")
    build.add_argument("--model")
    build.add_argument("--base-url")
    build.add_argument("--api-key-env", default="CLAUSEGRAPH_API_KEY")

    fuse = sub.add_parser("fuse")
    fuse.add_argument("--base", required=True)
    fuse.add_argument("routes", nargs=3)
    fuse.add_argument("--output", required=True)

    publish = sub.add_parser("publish")
    publish.add_argument("graph")
    publish.add_argument("--neo4j-uri", required=True)
    publish.add_argument("--database", default="neo4j")

    execute = sub.add_parser("run")
    execute.add_argument("--manifest", required=True)
    execute.add_argument("--route3", action=argparse.BooleanOptionalAction, default=None,
                         help="开启或关闭三路，覆盖清单中的 enable_route3（缺省关闭）")
    execute.add_argument("--output", required=True)
    execute.add_argument("--neo4j-uri")
    execute.add_argument("--database", default="neo4j")

    init = sub.add_parser("project-init", help="创建项目文件库")
    init.add_argument("--ontology", required=True)
    init.add_argument("--route3", action="store_true", help="默认开启新文件的三路")

    append = sub.add_parser("import", help="导入单个文件版本")
    append.add_argument("document")
    append.add_argument("--id", required=True)
    append.add_argument("--type")
    append.add_argument("--replace", action="store_true")
    append.add_argument("--route3", action=argparse.BooleanOptionalAction, default=None)

    files = sub.add_parser("project-files", help="列出项目文件版本")
    files.add_argument("--history", action="store_true")

    snapshot = sub.add_parser("snapshot-create", help="用选定的文件版本生成图谱快照")
    selection = snapshot.add_mutually_exclusive_group(required=True)
    selection.add_argument("--version", action="append")
    selection.add_argument("--all-active", action="store_true")
    snapshot.add_argument("--output", help="另存快照图 JSON")
    snapshot.add_argument("--top-k", type=int, help="每个条款写入快照的 BM25 出向边上限，默认保留全部命中")
    for command in (init, append, files, snapshot):
        command.add_argument("--project", required=True)
        command.add_argument("--neo4j-uri", required=True)
        command.add_argument("--database", default="neo4j")

    validate = sub.add_parser("validate")
    validate.add_argument("kind", choices=("tree", "graph", "manifest", "ontology"))
    validate.add_argument("input")
    validate.add_argument("--base", help="校验路线图时提供公共底图")

    schema = sub.add_parser("schema")
    schema.add_argument("kind", choices=("tree", "graph", "manifest"))
    return parser


def _dispatch(args):
    if args.command == "parse":
        write_json(args.output, parse_document(args.document, args.id, args.type))
    elif args.command == "base":
        write_json(args.output, build_base(args.dataset, [_read(ClauseTree, path) for path in args.trees]))
    elif args.command == "build":
        base = _read(Graph, args.base)
        validate_base(base)
        if args.route == "route1":
            graph = build_route1(base, top_k=args.top_k)
        elif args.route == "route2":
            if not args.ontology:
                raise ValueError("二路需要 --ontology")
            graph = build_route2(base, args.ontology)
        else:
            settings = ExternalModelSettings(model=args.model, base_url=args.base_url, api_key_env=args.api_key_env)
            graph = asyncio.run(build_route3(base, OntoRAGExternal(settings)))
        write_json(args.output, graph)
        return int(graph.status != "ready")
    elif args.command == "fuse":
        graph = fuse_graphs(_read(Graph, args.base), [_read(Graph, path) for path in args.routes])
        write_json(args.output, graph)
        return int(graph.status != "ready")
    elif args.command == "publish":
        print(json.dumps(publish_graph(_read(Graph, args.graph), args.neo4j_uri, database=args.database)))
    elif args.command == "run":
        report = asyncio.run(run(args.manifest, args.output, enable_route3=args.route3,
                                 neo4j_uri=args.neo4j_uri, database=args.database))
        print(json.dumps(report["publication"], ensure_ascii=False))
    elif args.command in {"project-init", "import", "project-files", "snapshot-create"}:
        connection = {"neo4j_uri": args.neo4j_uri, "database": args.database}
        if args.command == "project-init":
            result = init_project(args.project, args.ontology, enable_route3=args.route3, **connection)
        elif args.command == "import":
            result = asyncio.run(import_document(args.project, args.document, args.id, document_type=args.type,
                                                 replace=args.replace, enable_route3=args.route3, **connection))
        elif args.command == "project-files":
            result = list_project_files(args.project, history=args.history, **connection)
        else:
            result = create_snapshot(args.project, version_ids=args.version, all_active=args.all_active,
                                     top_k=args.top_k, output=args.output, **connection)
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == "validate":
        if args.kind == "ontology":
            load_ontology(args.input)
        elif args.kind == "graph":
            graph = _read(Graph, args.input)
            if graph.route == "base":
                validate_base(graph)
            elif graph.route == "fused":
                validate_fused(graph)
            else:
                if not args.base:
                    raise ValueError("校验路线图需要 --base")
                base = _read(Graph, args.base)
                nodes = validate_base(base)
                _validate_route(base, graph, nodes, _evidence_index(nodes))
        else:
            _read({"tree": ClauseTree, "manifest": Manifest}[args.kind], args.input)
    elif args.command == "schema":
        model = {"tree": ClauseTree, "graph": Graph, "manifest": Manifest}[args.kind]
        print(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        return _dispatch(args)
    except Exception as exc:
        print(f"clausegraph：{exc}", file=sys.stderr)
        return 1
