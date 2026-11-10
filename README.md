# ClauseGraph 条款图引擎

ClauseGraph 将 DOCX 或 Markdown 解析为版本化条款树，构建 BM25 相似关系、预定义本体关系和可选的内容本体关系。项目工作台支持管理文档版本、按文件子集生成 Neo4j 快照，以及按 BM25 Top K 查看全图。

需要 Python 3.12 或更新版本。从仓库根目录安装并启动：

```sh
python -m pip install -e '.[web]'
python -m uvicorn clausegraph.workbench:create_app --factory --host 127.0.0.1 --port 8765
```

工作台访问 [127.0.0.1:8765](http://127.0.0.1:8765/)，默认连接 `bolt://127.0.0.1:7688`。CLI、Python SDK、外部模型与 Neo4j 配置见[使用说明](docs/usage.md)。

| 位置 | 内容 |
| --- | --- |
| [src/clausegraph/](src/clausegraph/) | 当前引擎、CLI 和项目工作台 |
| [tests/](tests/) | 基于合成输入的自动化测试 |
| [docs/](docs/README.md) | 项目概览、架构、算法与使用说明 |
| [examples/](examples/) · [scripts/](scripts/) | 合成示例与性能基准 |

安装 `.[external,web]` 后可运行测试：

```sh
python -m unittest discover -s tests -v
CLAUSEGRAPH_TEST_NEO4J=bolt://127.0.0.1:7688 python -m unittest discover -s tests -v
```


## 公开仓库范围

本仓库包含引擎源码、通用技术文档、合成示例和合成测试。数据库、运行时数据、企业/项目原始材料、标准条款库和历史归档未包含在公开版本中。使用自己的文档或数据库前，请确认拥有相应的处理与分享授权。
