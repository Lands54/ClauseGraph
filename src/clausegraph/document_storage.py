"""Store parsed document trees as version-owned Neo4j nodes."""

from .contracts import ClauseTree


def tree_rows(tree):
    rows = []

    def visit(clause, parent=None):
        rows.append({
            "id": clause.clause_id, "parent": parent, "label": clause.title,
            "level": clause.level, "order": clause.order,
            "location": clause.location.model_dump() if clause.location else None,
            "blocks": [{
                **block.location.model_dump(), "order": order, "text": block.text if block.kind == "paragraph" else None,
                "rows": [{"row": row, "cells": cells} for row, cells in enumerate(block.rows, 1)],
            } for order, block in enumerate(clause.blocks, 1)],
        })
        for child in clause.children:
            visit(child, clause.clause_id)

    visit(tree.root)
    return rows


def write_tree(tx, version, rows):
    tx.run("""
        MATCH (v:ClauseGraphDocumentVersion {id:$version})
        UNWIND $rows AS row
        MERGE (c:Clause:VersionClause {version_id:$version, local_clause_id:row.id})
        SET c.kind='Clause', c.label=row.label, c.level=row.level, c.order=row.order,
            c.location_unit=row.location.unit, c.location_start=row.location.start, c.location_end=row.location.end
        FOREACH (_ IN CASE WHEN row.parent IS NULL THEN [1] ELSE [] END |
            MERGE (v)-[:HAS_ROOT]->(c)
        )
        """, version=version, rows=rows).consume()
    tx.run("""
        UNWIND $rows AS row
        WITH row WHERE row.parent IS NOT NULL
        MATCH (p:VersionClause {version_id:$version, local_clause_id:row.parent}),
              (c:VersionClause {version_id:$version, local_clause_id:row.id})
        MERGE (p)-[:CONTAINS]->(c)
        """, version=version, rows=rows).consume()
    tx.run("""
        UNWIND $rows AS row
        MATCH (c:VersionClause {version_id:$version, local_clause_id:row.id})
        UNWIND row.blocks AS block
        WITH c, block, CASE WHEN block.text IS NULL THEN 'Table' ELSE 'Paragraph' END AS kind
        MERGE (c)-[:HAS_BLOCK]->(b:ContentBlock {order:block.order})
        SET b:$(kind), b.kind=kind, b.unit=block.unit, b.start=block.start, b.end=block.end, b.text=block.text
        FOREACH (item IN block.rows |
            MERGE (b)-[:HAS_ROW]->(r:TableRow {row:item.row})
            SET r.kind='TableRow', r.cells=item.cells, r.text=item.text
        )
        """, version=version, rows=rows).consume()


def store_tree(tx, version, tree):
    write_tree(tx, version, tree_rows(tree))
    tx.run("""
        MATCH (v:ClauseGraphDocumentVersion {id:$version})
        SET v.diagnostics=$diagnostics
        """, version=version, diagnostics=tree.diagnostics).consume()
    if read_tree(tx, version) != tree:
        raise RuntimeError(f"文档条款树写入后与解析结果不一致：{version}")


def read_tree(tx, version):
    record = tx.run("""
        MATCH (v:ClauseGraphDocumentVersion {id:$version})
        RETURN v{.document_id,.document_type,.source_sha256,.parser_version,.diagnostics} AS version
        """, version=version).single(strict=True)["version"]

    rows = tx.run("""
        MATCH (v:ClauseGraphDocumentVersion {id:$version})-[:HAS_ROOT]->(:VersionClause)
              -[:CONTAINS*0..]->(c:VersionClause)
        OPTIONAL MATCH (p:VersionClause)-[:CONTAINS]->(c)
        CALL (c) {
            OPTIONAL MATCH (c)-[:HAS_BLOCK]->(b)
            CALL (b) {
                OPTIONAL MATCH (b)-[:HAS_ROW]->(r:TableRow)
                WITH r ORDER BY r.row
                RETURN collect(r.cells) AS cells
            }
            WITH b, cells ORDER BY b.order
            RETURN collect(CASE WHEN b IS NOT NULL THEN {
                kind:toLower(b.kind), location:{unit:b.unit,start:b.start,end:b.end},
                text:coalesce(b.text,''), rows:cells
            } END) AS blocks
        }
        RETURN c.local_clause_id AS id, p.local_clause_id AS parent, c.label AS title,
               c.level AS level, c.order AS ordering, blocks,
               CASE WHEN c.location_unit IS NOT NULL THEN
                   {unit:c.location_unit,start:c.location_start,end:c.location_end} END AS location
        ORDER BY c.order
        """, version=version).data()
    clauses = {}
    for row in rows:
        clause = dict(clause_id=row["id"], title=row["title"], level=row["level"],
                      order=row["ordering"], location=row["location"], blocks=row["blocks"], children=[])
        clauses[row["id"]] = clause
        if row["parent"] is None:
            root = clause
        else:
            clauses[row["parent"]]["children"].append(clause)
    if not rows:
        raise ValueError(f"文档版本缺少条款树：{version}")
    return ClauseTree.model_validate({**record, "root": root})
