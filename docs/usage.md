# 通用条款图引擎

用 Python 3.12 或更新版本安装。在仓库根目录运行：

```sh
python -m pip install '.[external,neo4j]'
```

三路默认关闭。运行时传入 `--route3`，或在清单设置 `"enable_route3": true` 即可开启。CLI 的 `--no-route3` 可覆盖清单设置，SDK 可传入 `enable_route3=True` 或 `False`。

开启三路时，设置 `CLAUSEGRAPH_BASE_URL`、`CLAUSEGRAPH_MODEL` 和 `CLAUSEGRAPH_API_KEY`。基础地址是模型服务的 HTTPS API 前缀（通常以 `/v1` 结尾）。服务需支持 Chat Completions 和 JSON object 响应格式。三路会将抽取所需的标题、段落和表格行发送到所配置的服务。清单中的 `model` 对象也可显式设置 `base_url`、`model`、`timeout` 和密钥环境变量名 `api_key_env`。密钥只从环境读取。

```sh
clausegraph run --manifest examples/clausegraph/manifest.json --output run-output --route3
clausegraph publish run-output/fused.json --neo4j-uri bolt://127.0.0.1:7688
```

也可给 `run` 直接传入 `--neo4j-uri`。Neo4j 凭据通过 `NEO4J_USER` 和 `NEO4J_PASSWORD` 提供，目标库通过 `--database` 选择。发布要求所有已开启的路线完成。关闭三路时，报告将其标记为 `skipped`，可直接发布一路与二路的结果。数据库写入失败后，可以用保留的 `fused.json` 重试发布。每次构建使用新建或空的输出目录。阶段诊断和发布状态在 `report.json` 中。

```python
import asyncio

from clausegraph import run

report = asyncio.run(run("examples/clausegraph/manifest.json", "sdk-output"))
print(report["graphs"]["fused"])
```

## 模块输入和产物

- `parse` 接收带显式标题的 DOCX、Markdown 或规范条款树 JSON。Markdown 支持标题、代码块和管道表格。DOCX 支持文档主体中的段落和表格，并解析直接大纲级别及样式继承。条款树保留直属内容和原文定位，DOCX 用主体块序号，Markdown 用行范围，表格行从 1 开始。清单中路径相对于清单文件解析。
- `base` 以一组条款树创建公共图。各路通过 `build --route route1|route2|route3` 接收同一公共图。二路需要 `--ontology`。三路可从环境读取外部模型配置，也支持 `--base-url`、`--model`、`--api-key-env`。
- 一路在输入集合内生成有向 BM25 邻居。二路加载 Turtle 中的命名概念、关系和规则。规则支持 `matchTitleCue`、`matchBodyCue`、`matchHeaderCue`、`matchIdentifierPattern` 和 `alias`，以及可选的 `allowedDocumentType`。匹配时各组最多计一次分，命中内容保留为证据。三路通过 OntoRAG 提取内容概念和关系，并保留证据和核验状态。
- `fuse` 合并三路断言。发布到 Neo4j 后，快照通过 `HAS_NODE` 关联图节点。断言的 `evidence_refs` 指向对应文档版本中的原文。节点和关系字段见 [Neo4j 数据契约](neo4j-contract.md)。

```sh
clausegraph schema tree > clause-tree.schema.json
clausegraph schema graph > graph.schema.json
clausegraph schema manifest > manifest.schema.json
clausegraph validate graph run-output/fused.json
```

```cypher
MATCH (s:ClauseGraphSnapshot {id:$snapshot})-[:HAS_NODE]->(a:Clause)
      -[r:MENTIONS]->(b:ContentConcept)
MATCH (a)-[:IN_DOCUMENT]->(:Document)-[:REPRESENTS_VERSION]->(v:ClauseGraphDocumentVersion)
RETURN v.document_id, v.source_sha256, b.label, r.assertions_json
```

安装后可从任意目录运行通用包，输入文件由清单提供。

测试结果、规模基线和复现命令见[验收记录](verification.md)。

## 项目文件库与快照

项目保存文件版本及其条款树、二路和三路产物。导入时可用 `--route3` 或 `--no-route3` 覆盖项目的默认设置。相同文档 ID 的新版本需用 `--replace`，成功后旧版本从活动列表移出。历史快照继续引用原版本。

```sh
clausegraph project-init --project cooling-example --ontology examples/clausegraph/ontology.ttl \
  --neo4j-uri bolt://127.0.0.1:7688
clausegraph import examples/clausegraph/notes.md --project cooling-example --id notes \
  --neo4j-uri bolt://127.0.0.1:7688
clausegraph project-files --project cooling-example --history --neo4j-uri bolt://127.0.0.1:7688
clausegraph snapshot-create --project cooling-example --all-active \
  --neo4j-uri bolt://127.0.0.1:7688
```

快照也可用一个或多个 `--version <版本ID>` 选择活动文件子集。构建时运行一路 BM25，并合并已保存的二路和三路产物。快照默认保存所有有词项命中的有向 BM25 边。生成时可用 CLI 的 `--top-k 12` 或 SDK 的 `create_snapshot(..., top_k=12)` 限制每个条款写入的 BM25 出向边。工作台 API 的请求体可传正整数 `top_k`，省略或传 `null` 表示全量。下游可按 Neo4j 边属性 `r.rank` 和 `r.bm25` 过滤，排名从 1 开始，SQLite FTS5 原始分数越小越靠前。通过返回的快照 ID 查询图谱。

```cypher
MATCH (:ClauseGraphSnapshot {id:$snapshot})-[:HAS_NODE]->(a:Clause)-[r:SIMILAR_TO]->(b:Clause)
MATCH (a)-[:IN_DOCUMENT]->(da:Document), (b)-[:IN_DOCUMENT]->(db:Document)
WHERE da.id <> db.id
RETURN a,r,b LIMIT 80
```

二路的跨文档路径通过两侧的 `CLASSIFIED_AS` 和共享或相连的本体概念查询。

## 本地项目工作台

```sh
python -m pip install '.[web]'
python -m uvicorn clausegraph.workbench:create_app --factory --host 127.0.0.1 --port 8765
```

访问 [项目工作台](http://127.0.0.1:8765/)。工作台默认连接本机的 `bolt://127.0.0.1:7688`，可用 `CLAUSEGRAPH_NEO4J_URI` 和 `CLAUSEGRAPH_NEO4J_DATABASE` 指定连接。需要三路时安装 `.[web,external]` 并配置外部模型环境变量。

页面支持新建项目、上传文件、替换活动版本和查看历史。勾选活动版本后可生成快照。生成前可填「快照 BM25 Top K」，留空保存全量。每个快照可生成四条 Cypher，分别查看条款树（含段落、表格和行）、1 路 BM25、2 路本体和 3 路内容抽取。复制到 Neo4j Browser 后切换到 Graph 视图。1 路查询的 BM25 Top K 默认是 1，填 0 隐藏 BM25，留空显示快照已保存的全部 BM25 边。

快照旁的「下载配色」会生成 Neo4j Browser 的 GraSS 文件。在 Browser 运行 `:style`，上传下载的文件。文档节点和该文档的条款使用同一种颜色，预定义本体为紫色，三路内容概念为青绿色。若 Browser 优先显示通用节点颜色，可在结果概览把 `Doc_…`、`TypeConcept` 和 `ContentConcept` 标签移到通用标签前面。

## 文本、候选和结果上限

三路按文件提交，从解析结果按原顺序拼接标题、正文和表格内容。每份文件调用一次 OntoRAG 学习流程，保留文件内部的跨条款上下文。该流程内部仍可发起多次模型请求。适配器在上游任务提示缺少全文时补齐上下文，请求不设置 `max_tokens` 或 `max_completion_tokens`。服务返回长度截断时，按文件报错。三路当前仍按 `min_confidence=0.55` 筛选 OntoRAG 候选。

输入在每个条款的直属内容前标记 `[clause_id=…]`。模型在原有术语抽取请求中返回 `mentions`（每项含 `concept` 和 `clause_id`），无需重述原文。三路按 ID 在当前文档版本中挂载，重复挂载只保留一次，无效 ID 记入路线图的 `unresolved_mentions`。挂载状态为 `pending`，证据从本地取条款起始位置供回查；该位置不代表模型选中了某段原文。自定义 SDK 提供方可通过 `Extraction.mentions` 返回 `ClauseMention`。

| 位置 | 当前限制 |
| --- | --- |
| `clausegraph` 一路 | 项目快照、批处理和独立构建默认保留全部有词项命中的边。快照生成、批处理和独立构建均可显式设置正整数上限。 |
| `clausegraph` 二路 | 本体规则要求命中，且分数达到概念阈值。确认状态还要求达到项目本体的分数和命中组数阈值。 |
| `clausegraph` 三路 | 上游锁定版本的术语、分类、关系提示仍有 2,000 字符切片，术语类型判断为 500 字符且 `top_k=1`。适配器给模型补齐全文。实际上下文和输出上限由模型服务决定。默认请求超时为 120 秒。 |
| 工作台 | 上传单文件超过 32 MiB 会拒绝。快照列表最多显示 50 个，预览查询分别最多返回 80 条跨文档 BM25 边和 40 条本体路径，本体预览只走共享概念或一跳类型关系。 |
