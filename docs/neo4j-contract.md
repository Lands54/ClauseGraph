# Neo4j 数据契约 v2

Neo4j 用标签和原生 `kind` 区分节点类型，用关系名表达业务含义。常查字段直接存为属性；原文统一从文档版本回查。

新发布使用本契约。可移植图 JSON 使用 `schema_version: 2`，直接使用同样的分析关系名。条款树 JSON 仍使用版本 1。

## 节点

`kind` 等于下表的主标签。四类快照图节点另带 `ClauseGraphNode` 标签，共用 `id`（原图 ID）、`snapshot_id`、`key`（`snapshot_id:id`）和 `label`。带 `?` 的字段可省略。发布器通过 [图节点属性模型](../src/clausegraph/neo4j_nodes.py) 校验原生字段的类型和必填项，并拒绝未声明的字段。

| 主标签 / `kind` | 专属原生字段 | 含义 |
| --- | --- | --- |
| `ClauseGraphProject` | `id`、`revision`、`enable_route3`、`ontology` | 管理文档版本、快照和项目本体；模型配置保存在 `model_config_json` |
| `ClauseGraphSnapshot` | `id`、`dataset_id`、`storage_schema_version=2`、`graph_fingerprint`、`base_fingerprint`、`config_fingerprint`、`created_at`、`status=ready` | 一次不可变发布；路线详情可存入 `metadata_json` |
| `ClauseGraphDocumentVersion` | `id`、`origin`（`project` / `standalone`）、`document_id`、`document_type?`、`source_sha256`、`parser_version`、`route3_status?` | 文档版本元数据和原文来源的归属 |
| `Document` | — | 文档版本在快照中的入口 |
| `Clause` | `local_clause_id`、`level`、`order`、`location_unit?`、`location_start?`、`location_end?` | `label` 是标题；定位字段成组出现，根条款无标题定位，层级和顺序均为 0 |
| `TypeConcept` | `uri`、`labels`（字符串列表）、`document_types`（字符串列表） | 项目本体预定义的概念；空文档类型列表表示不限类型 |
| `ContentConcept` | — | 从某篇文档抽取的概念；`label` 是概念名称 |

文档 ID、源文件哈希和解析器版本统一查版本节点。每个文档版本通过 `HAS_ROOT` 连接一棵原生条款树。树上的 `Clause:VersionClause` 以 `version_id` 和 `local_clause_id` 定位，保存标题、层级、顺序和标题位置。父子条款沿 `CONTAINS` 相连。

正文按解析顺序通过 `HAS_BLOCK` 连接。快照条款通过 `REPRESENTS_CLAUSE` 指向版本条款，由版本条款独占 `HAS_BLOCK`，各快照通过版本条款读取同一份正文。

| 内容节点 | 原生字段 |
| --- | --- |
| `Paragraph` | `kind`、`order`、`unit`、`start`、`end`、`text` |
| `Table` | `kind`、`order`、`unit`、`start`、`end`，通过 `HAS_ROW` 连接行 |
| `TableRow` | `kind`、`row`、`cells`（字符串列表）。仅凭总图独立发布时，使用总图已有的行文本 `text` |

项目文档保留 `route2_json` 和 `route3_json` 供快照构建复用。导入时直接写入原生条款树。独立发布时，用总图中的来源生成原生正文节点。

```cypher
MATCH (s:ClauseGraphSnapshot {id: $snapshot_id})-[:HAS_NODE]->(c:Clause)
WHERE c.local_clause_id = $clause_id
MATCH (c)-[:REPRESENTS_CLAUSE]->(:VersionClause)-[:HAS_BLOCK]->(b:ContentBlock)
OPTIONAL MATCH (b)-[:HAS_ROW]->(r:TableRow)
RETURN c.label, b.order, b.kind, b.text, r.row, r.cells
ORDER BY b.order, r.row
```

## 关系

| 业务关系 | 方向 | 读法与属性 |
| --- | --- | --- |
| `CONTAINS` | `Document` → 根 `Clause`；父 `Clause` → 子 `Clause` | 文档或条款包含下一级；每个条款只有一个父节点 |
| `SIMILAR_TO` | `Clause` → `Clause` | 一路：条款与另一条款相似；有方向，无自环，边上保存 `predicate=bm25`、`bm25`、`rank`，证据覆盖两端 |
| `CLASSIFIED_AS` | `Clause` → `TypeConcept` | 二路：规则将条款归入某个本体概念；断言保留 `rule_score`、`review_status`、`matched_cues`。待复核的分类仍按原状态展示 |
| `MENTIONS` | `Clause` → `ContentConcept` | 三路：抽取结果认为条款提及某个内容概念；断言保留 `review_status`、`method`、`evidence_scope`。条款锚点只用于定位 |
| `TYPE_RELATION` | `TypeConcept` → `TypeConcept` | 两个本体概念之间的定义关系；`predicate` 是本体谓词 URI，无原文证据 |
| `CONTENT_RELATION` | `ContentConcept` → `ContentConcept` | 同一文档中抽取的概念关系；保存 `predicate`，断言保留复核状态和证据引用 |

二路输出 `CLASSIFIED_AS`，三路输出 `MENTIONS`。

| 管理与归属关系 | 方向 | 约束 |
| --- | --- | --- |
| `HAS_VERSION` | 项目 → 文档版本 | 无 `retired_at` 表示活动；每个项目的同一文档最多一个活动版本 |
| `HAS_SNAPSHOT` | 项目 → 快照 | 项目快照有一个所属项目；独立快照没有 |
| `USES_DOCUMENT` | 快照 → 文档版本 | 记录该快照实际使用的版本，历史快照可引用已退役的版本 |
| `HAS_NODE` | 快照 → 图节点 | 每个图节点只属于一个快照 |
| `REPRESENTS_VERSION` | `Document` → 文档版本 | 每个文档图节点对应一个版本；必须在快照的 `USES_DOCUMENT` 集合中 |
| `IN_DOCUMENT` | `Clause` / `ContentConcept` → `Document` | 直接查所属文档；条款归属由 `CONTAINS` 层级派生 |
| `HAS_ROOT` | 文档版本 → 根 `VersionClause` | 每个原生文档版本一棵条款树 |
| `REPRESENTS_CLAUSE` | 快照 `Clause` → `VersionClause` | 通过文档版本和 `local_clause_id` 对应唯一的版本条款 |
| `HAS_BLOCK` | `VersionClause` → `Paragraph` / `Table` | 只有版本条款持有正文，内容块按 `order` 排序 |
| `HAS_ROW` | `Table` → `TableRow` | 表格行按 `row` 排序 |

业务边不得跨快照。条款与其提及的内容概念必须同文档；分类和提及的证据来自源条款。结构边不保存断言。

## 证据怎么查

语义边的 `assertions_json` 保留每条断言的 `route`、`details` 和 `evidence_refs`。关系类型、端点、谓词以及原生 BM25 分值和排名不在断言中重存。

证据引用包含条款 ID、来源位置和摘录范围。先用 `REPRESENTS_CLAUSE` 找到版本条款，再从标题或 `HAS_BLOCK → HAS_ROW` 查来源。项目导入的表格行以 ` | ` 拼接单元格后应用偏移，独立发布的表格行直接使用 `text`。来源位置使用现有 `docx_block` 或 `markdown_line`，首尾均从 1 起且包含端点，表格行号同样从 1 起；摘录范围用 Unicode 码点偏移，从 0 起、结束不含。转换旧证据时取指定来源内的首个精确匹配，无法回查则拒绝发布。条款不再保存 `sources`，关系也不再单独存 `evidence_json`。

例如，查询某个快照里归入指定本体概念的条款：

```cypher
MATCH (:ClauseGraphSnapshot {id: $snapshot_id})-[:HAS_NODE]->(c:Clause)
      -[:CLASSIFIED_AS]->(t:TypeConcept {uri: $uri})
MATCH (c)-[:IN_DOCUMENT]->(:Document)-[:REPRESENTS_VERSION]->(v:ClauseGraphDocumentVersion)
RETURN v.document_id, c.label, t.label
ORDER BY v.document_id, c.order
```

## 发布与重建

- 项目、快照、版本各自以 `id` 唯一；图节点以 `key` 唯一。业务边按方向、端点和关系 `key` 幂等写入；`key` 沿用输入图四元组（原关系类型、源、目标、谓词）的指纹。
- 项目保留现有版本 ID。独立版本 ID 由 `standalone`、存储版本、文档 ID、源文件哈希、解析器版本、文档类型及规范化来源索引计算。v2 快照 ID 由存储版本、原总图指纹、项目 ID（独立发布为空）和排序后的版本 ID 计算。
- 旧版项目和总图需要从原文重新导入并生成快照。新版文档版本通过 `HAS_ROOT` 提供原文，快照条款通过 `REPRESENTS_CLAUSE` 回查。
- 清理旧项目时按明确的项目 ID 列表处理。先核对项目的快照和文档版本，并导出需要保留的内容。删除快照节点和项目归属后，再清理已无项目或快照引用的文档版本及其原文树。独立发布的快照单独盘点。
