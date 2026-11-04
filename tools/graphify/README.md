# Graphify 本地试用工具

Graphify 0.9.94 已安装到独立虚拟环境，用于代码关系和文档结构检索。F1-infra 首轮试用已建图：符号起点查询有用，中文宽泛提问、HTTP 链路及配置覆盖仍有缺口，暂作为辅助索引使用。

试用结论和完整复现方法见 [F1-infra 复盘](../WhyHow/content/graphify-f1-infra/README.md)，人类入口为 [Why&How 试用复盘](http://192.0.2.1:4243/?doc=graphify-f1-infra-review)和[关系图](http://192.0.2.1:4243/?html=graphify-f1-infra)。项目源码和服务保持原状；图谱是2026-10-23的快照，变更后需要刷新。

## 文件与运行

- `.venv/bin/graphify`：原生 CLI；没有安装常驻 skill、AGENTS 指令或 Git hook。
- `requirements.lock.txt`：本次已安装版本。
- `build_local_graph.py`：调用原生结构解析器，包含 Markdown 标题/链接，不调用语义模型。
- `vendor/`：固定版本图形依赖与许可证，用于离线展示。
- `../../Artifacts/graphify-f1-infra-20261023/`：源快照、输入哈希、原生图、覆盖与查询证据。

```bash
cd /home/user/Tools/graphify
./.venv/bin/graphify --version
./.venv/bin/graphify --help
```

未来对新的源码快照运行 `build_local_graph.py --source <快照目录> --output <新产物目录>`，再使用 `graphify cluster-only <快照目录> --graph <新产物目录>/graphify-out/graph.json --no-label` 生成原生图和报告。需要更新阅读入口时，按 WhyHow 的 catalog 与定向发布规则操作。
