---
doc_type: reference
domain: training
status: canonical
created: 2026-07-28
updated: 2026-08-24
models: [dm05, old_lora, new_lora]
hardware: []
canonical_for: [upstream_customization_map]
supersedes: [05_CUSTOMIZATION_MAP.md]
superseded_by: []
related: []
---

# G2 相对 OpenDM 上游改造索引

基线：OpenDM commit `6ce8e7f13e0c46906b141fda1773364f18a577c2`。

## 必要的上游核心补丁

| 文件 | 变化 | 原因 | 验证 |
|---|---|---|---|
| `opendm/opendm/model/dm05/dm05_arch.py` | 新增动作投影resize | 保留官方32D权重，仅初始化新增2维 | 权重保持与梯度测试 |
| `opendm/opendm/model/dm05/dm05_lora.py` | 调整base/adapter加载顺序 | 避免34D配置导致官方投影整体重建 | adapter加载路径检查 |
| `opendm/opendm/exp/dm05_exp.py` | 模型、数据、推理显式action_dim | 防止PadAction或推理截断为32D | 维度一致性检查 |
| `opendm/opendm/dataset/g2_sort.py` | 新增数据集桥接层 | 注册三路图像和34D state | 真实loader smoke |

所有修改区域均保留上游32D默认行为；G2 recipe显式选择34D。

## 项目新增代码

| 路径 | 责任 |
|---|---|
| `g2_sort/config/action_contract.json` | 34D顺序、语义、H5来源和固定prompt |
| `g2_sort/training/dm05_lora.py` | G2 LoRA训练与推理配置 |
| `g2_sort/data_tools/index_zip_archive.py` | ZIP中央目录与metadata索引 |
| `g2_sort/data_tools/index_tar_archive.py` | tar.gz单遍流式索引和原子报告 |
| `g2_sort/data_tools/`其余工具 | episode审计、转换、split和动态分析 |
| `g2_sort/deployment/` | 无执行器调用的shadow客户端 |
| `g2_sort/validation/` | checkpoint交付合同检查 |
| `g2_sort/tests/` | G2关键回归测试 |
| `infra/docker/Dockerfile.opendm-g2` | 本地/服务器可配置Docker环境 |
| `infra/host/` | base下载、归档复制和宿主机环境脚本 |

## 非源码产物

`data/raw_samples`、`data/processed`和`data/audits`不属于OpenDM代码。
`__pycache__`、`.pytest_cache`、模型权重和训练checkpoint均应忽略。

## 目录边界

- `opendm/`只保留固定上游及不可避免的34D核心补丁；
- `g2_sort/`保存G2专用训练、数据、验证和部署代码；
- `infra/`保存Docker及宿主机脚本；
- `server/`是NVMe bind mount后的项目内统一入口，并被Git忽略；
- `docs/`只保存小型、可审计的设计与实施记录。

历史结构重组已经完成，不再保留一份长期“重组计划”；实际目录和本改造索引
就是当前结构的权威记录。
