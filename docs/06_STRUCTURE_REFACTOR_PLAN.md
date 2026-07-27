# DM0.5 × G2 项目结构重组方案

状态：已完成
记录日期：2026-07-27

完成记录：目录边界、模块说明、Docker入口、路径引用和改造索引均已重组；
重组后的独立镜像、3项单元测试、训练recipe导入及真实三路数据smoke均通过。

## 一、重组目标

当前项目根目录同时包含：

- 固定版本的 OpenDM 上游仓库；
- 对 OpenDM 核心代码所做的 34D G2 适配；
- G2 数据审计、转换、训练、验证和部署工具；
- 项目计划与实施记录；
- 原始样本、审计报告及转换后的 smoke 数据。

这些内容目前分散在项目根目录和 OpenDM 仓库内部，不利于快速判断文件来源、
维护责任和是否属于可重新生成的产物。

本次重组的目标是建立清晰边界：

1. `opendm/` 只表示固定 commit 的上游仓库及不可避免的核心补丁；
2. `g2_sort/` 集中放置本项目专用代码；
3. `data/` 统一管理样本、审计、manifest 和转换产物；
4. `infra/` 管理 Docker、宿主机设置和环境依赖；
5. `docs/` 管理计划、状态、审计、运行手册和改造索引；
6. 每个 G2 Python 文件都能从文件头看懂用途、输入、输出和关键约束。

## 二、必须保留的上游目录结构

`opendm/opendm/` 的双层命名不是当前项目造成的冗余：

- 外层 `opendm/` 是 Git 仓库根目录；
- 内层 `opendm/opendm/` 是 Python package，对应 `import opendm`。

不能直接将内层 package 摊平，否则会破坏官方导入路径、editable install、
测试和后续与上游版本进行差异比较的能力。

## 三、目标目录结构

```text
DM0.5/
├── README.md
│
├── docs/
│   ├── 01_REPRODUCTION_PLAN.md
│   ├── 02_IMPLEMENTATION_STATUS.md
│   ├── 03_DATA_AUDIT.md
│   ├── 04_TRAINING_DEPLOYMENT.md
│   ├── 05_CUSTOMIZATION_MAP.md
│   └── 06_STRUCTURE_REFACTOR_PLAN.md
│
├── infra/
│   ├── docker/
│   │   └── Dockerfile.opendm-g2
│   ├── host/
│   │   └── setup_nvidia_container_toolkit.sh
│   └── requirements/
│       └── data-tools.txt
│
├── g2_sort/
│   ├── README.md
│   ├── config/
│   │   └── action_contract.json
│   ├── training/
│   │   └── dm05_lora.py
│   ├── data_tools/
│   │   ├── audit_episode.py
│   │   ├── index_archive.py
│   │   ├── convert_episode.py
│   │   ├── build_split.py
│   │   ├── process_split.py
│   │   └── analyze_dynamics.py
│   ├── deployment/
│   │   └── shadow_inference_client.py
│   ├── validation/
│   │   └── validate_checkpoint.py
│   └── tests/
│       ├── test_action_resize.py
│       └── test_split.py
│
├── data/
│   ├── README.md
│   ├── raw_samples/
│   ├── processed/
│   │   └── smoke/
│   ├── audits/
│   └── manifests/
│
└── opendm/
    └── opendm/
        └── dataset/
            └── g2_sort.py
```

## 四、文件归类原则

### 4.1 保留在 OpenDM 仓库内的内容

以下代码直接参与 OpenDM 的模型加载、训练配置或数据集注册，保留在 fork
中更可靠：

| 文件 | 类型 | 保留原因 |
|---|---|---|
| `opendm/opendm/model/dm05/dm05_arch.py` | 修改上游 | 负责将官方 32D 动作投影扩展到 34D |
| `opendm/opendm/model/dm05/dm05_lora.py` | 修改上游 | 负责 base、34D 密集层和 LoRA 的正确加载顺序 |
| `opendm/opendm/exp/dm05_exp.py` | 修改上游 | 让训练、数据和推理使用统一的可配置动作维度 |
| `opendm/opendm/dataset/g2_sort.py` | 项目新增桥接层 | 将 G2 数据集注册到 OpenDM |

这些文件中的项目修改必须添加 `G2 34D EXTENSION` 注释，说明：

- OpenDM 上游的原始行为；
- 为什么需要修改；
- 修改后保证的兼容性；
- 哪些权重继承，哪些维度重新初始化；
- 训练、保存和推理必须共同遵守的边界。

### 4.2 移出 OpenDM 仓库的项目专用内容

以下内容不需要侵入上游源码，应集中移动到根目录 `g2_sort/`：

- G2 LoRA recipe；
- 34D action contract；
- ZIP/tar 元数据索引工具；
- H5 审计和动态分析工具；
- episode 转换和 split 工具；
- shadow inference 客户端；
- checkpoint 合同检查工具；
- G2 专用单元测试。

重组后，`opendm/` 的 Git diff 应尽可能只剩不可避免的核心兼容修改和数据集
注册桥接层。

### 4.3 Docker 与依赖

项目定制 Dockerfile 移到 `infra/docker/Dockerfile.opendm-g2`，构建上下文
使用项目根目录。这样可以：

- 保持官方 `opendm/Dockerfile` 易于比较；
- 明确国内 apt、Conda、PyPI 镜像属于本项目基础设施；
- 明确 FFmpeg、H5 工具和本地 CUDA 设置不是上游默认行为。

只供数据工具使用的依赖放入 `infra/requirements/data-tools.txt`，避免把
所有预处理依赖无条件写入 OpenDM 核心 Python 依赖。

当前正在进行的首次 Docker 构建完成之前，不修改其正在使用的 Dockerfile。
镜像构建完成并验证后，再迁移 Dockerfile；已生成的镜像和缓存层继续保留。

### 4.4 数据与生成产物

当前目录的映射关系：

| 当前目录 | 重组后目录 | 性质 |
|---|---|---|
| `data_samples/` | `data/raw_samples/` | 从共享盘抽取的真实样本 |
| `data_prepared_smoke/` | `data/processed/smoke/` | 可重新生成的转换产物 |
| `data_audit/` | `data/audits/` | 审计证据和诊断报告 |
| 后续 split manifest | `data/manifests/` | 正式训练输入边界 |

`data/README.md` 必须说明：

- 数据来自哪个共享盘归档；
- 哪些是原始抽取样本；
- 哪些文件可由脚本重新生成；
- 哪些 manifest 和审计报告属于需要保留的证据；
- 不直接从 SMB 共享盘训练，也不向共享盘写转换产物。

## 五、Python 文件头规范

现有一行英文 docstring 不足以承担项目交接说明。所有 G2 Python 文件统一
采用模块级说明，至少包含：

```python
"""
模块名称和一句话目标。

用途：
    这个文件解决什么工程问题。

输入：
    需要的数据、配置或服务。

输出：
    生成的文件、报告或模型接口。

关键约束：
    时间戳、34D 顺序、安全模式或不能静默退化的条件。

归属：
    G2 DM0.5 项目定制代码，不属于 OpenDM 上游。
"""
```

具体要求：

- 数据工具说明是否会解码视频、是否修改原始数据；
- 转换工具明确 H5 帧号不能直接作为视频帧号；
- 训练 recipe 明确三路 RGB、34D absolute joint、固定 prompt 和 LoRA；
- inference 客户端明确当前只能 shadow mode，不得发送真机动作；
- checkpoint 检查明确 base、LoRA、34D 密集模块和 norm stats 的交付合同；
- 测试文件说明其防止的回归，而不是只保留测试函数名称；
- 核心代码只在设计边界添加解释，不增加无意义的逐行注释。

## 六、项目入口与定制索引

### 6.1 根目录 README

根目录 `README.md` 应成为唯一入口，至少回答：

1. 项目最终任务是什么；
2. 当前处于哪个阶段；
3. OpenDM 固定在哪个 commit；
4. 哪些目录是上游、项目代码、数据和文档；
5. 如何找到当前状态、数据审计和训练运行手册；
6. 当前没有下载 base 权重这一事实。

### 6.2 `docs/05_CUSTOMIZATION_MAP.md`

建立逐文件改造索引，字段至少包括：

| 文件 | 上游/新增 | 修改目的 | 输入输出合同 | 验证证据 |
|---|---|---|---|---|

该文件负责回答“相对官方 OpenDM，我们到底改了什么”，不能只依赖
`git status` 或个人记忆。

## 七、缓存和版本管理

- 清理 `__pycache__/` 和 `.pytest_cache/`；
- 确认缓存、临时转换数据和大模型权重均被 `.gitignore` 排除；
- 不删除真实样本、审计证据或 manifest；
- OpenDM 保持在 `g2-dexterous-lora` 分支；
- 重组后记录一次清晰的 Git diff 和完整测试结果；
- 如果后续建立父项目 Git 仓库，再决定使用 submodule 还是固定 fork commit，
  本次不擅自初始化新的远程仓库。

## 八、执行顺序

1. 等待当前 Docker 镜像完成并保存已有缓存；
2. 创建 `docs/`、`infra/`、`g2_sort/` 和 `data/`；
3. 移动项目文档并建立根 README；
4. 移动 G2 recipe、合同、工具和测试；
5. 仅保留 OpenDM 内必要的核心补丁和数据集桥接层；
6. 补充所有 G2 Python 文件的完整模块说明；
7. 给 OpenDM 三个核心修改区域增加 `G2 34D EXTENSION` 设计注释；
8. 更新所有路径、导入、Docker构建命令和文档链接；
9. 清理缓存并完善 ignore 规则；
10. 重新运行单元测试、真实 JSONL loader、三路图像 transform smoke；
11. 生成 `docs/05_CUSTOMIZATION_MAP.md` 和最终目录树；
12. 确认功能无回归后更新实施状态。

## 九、重组验收条件

- 从根目录 README 可以在一分钟内理解项目主线；
- 无需运行 `git status` 也能区分上游、G2定制代码和数据产物；
- 每个 G2 Python 文件从文件头即可理解用途、输入、输出和关键约束；
- OpenDM 核心修改均能追溯到明确的 34D 设计原因；
- 所有旧路径都已更新，不存在失效命令；
- 单元测试、数据审计、真实 JSONL loader 和图像 transform smoke 均通过；
- 不下载或移动 DM0.5 base 权重；
- 不改变当前已冻结的 34D action contract、固定 prompt 和第一版任务范围。
