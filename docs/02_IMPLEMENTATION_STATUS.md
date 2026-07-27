# DM0.5 × G2 实施状态

更新时间：2026-07-27

## 已完成

- 主计划保存为 `docs/01_REPRODUCTION_PLAN.md`。
- OpenDM 固定在 commit
  `6ce8e7f13e0c46906b141fda1773364f18a577c2`，实施分支为
  `g2-dexterous-lora`。
- 未下载 DM0.5 base 权重。
- 本机确认是 24 GB RTX 4090D；Docker Engine 28.2.2 正常，`bruno`
  已加入 docker 组。
- NVIDIA Container Toolkit 1.19.1 已安装，Docker `nvidia` runtime 已注册；
  现有容器内实测识别 `RTX 4090 D / 24564 MiB / driver 570.211.01`。
- 本地精简开发镜像 `opendm:g2-34d-6ce8e7f` 已按重组后的项目根目录重新
  构建，镜像 ID `sha256:22036f28d069...`；包含 CUDA 12.8.1、
  PyTorch 2.11.0+cu128 和完整 OpenDM Python 依赖，但不包含 DM0.5
  base 权重。
- 本地 G2 recipe 使用 eager/SDPA，因此 Docker 默认跳过可选
  FlashAttention 源码编译；服务器 GPU 型号确定后可显式启用并只针对
  所需架构构建。
- G2 34D absolute-joint 合同已保存到
  `g2_sort/config/action_contract.json`。
- 模型和数据配置支持显式 `action_dim=34`。
- 32D 官方 action projection 先按原尺寸加载，再扩到 34D；旧 32D 权重
  完整继承，仅新增维度初始化。
- LoRA adapter 推理加载遵循同样的“先加载 32D base，再扩头，再加载
  adapter”路径。
- G2 LoRA recipe 使用三路 RGB、absolute action、state 输入、无主动图像
  augmentation、50-step chunk、34D 输出。
- 已实现 ZIP 全量元数据索引、H5 全量轻量审计、95/5 episode split、
  H.265 无重编码 MP4 封装和显式-action JSONL 转换工具。
- 已实现只记录输出、不连接机器人执行器的 shadow HTTP 客户端。
- 已实现 checkpoint 34D/norm stats/LoRA 密集模块合同检查。

## 当前验证证据

- 34D action projection 测试：原 32D 权重保持，新增两维梯度非零。
- H5 审计：对 1018 帧样本完整读取，34D state/action、时间戳和有限数检查
  通过。
- JSONL 转换：生成 1018 行，每行显式包含 34D state 和 34D action。
- split 测试：97 个有效 episode 得到 92 train / 5 validation，集合无交叉。
- 本地测试：`3 passed`。
- 新镜像内测试：`3 passed in 2.86s`；容器内 PyTorch 成功识别
  `NVIDIA GeForce RTX 4090 D`。
- `record_20260409.zip` 全量中央目录和 401 个 `meta_info.json` 已索引，
  未解码视频；367 个 episode 通过归档级预检查。
- 已从共享盘提取一个 94 MB 必需字段真实 episode：完整 H5 审计通过；
  三路视频时间戳精确映射后生成 977 行显式 34D JSONL 和 53 MB MP4 smoke
  数据。验证了 H5 帧号不能直接当视频帧号，转换器已强制按时间戳查表。
- 新镜像内真实 OpenDM loader smoke 通过：随机检查第 0、488、975 个样本，
  三路视频均成功随机定位并补边 resize 到 `448×448`，state 为 34D，
  action chunk 为 `1×50×34`，最后两维未被截断。
- 项目结构重组完成：G2代码集中在 `g2_sort/`，数据集中在 `data/`，
  基础设施集中在 `infra/`，文档集中在 `docs/`；OpenDM工作树只保留三个
  必要的34D核心修改和一个数据集注册桥接层。
- 公开Git仓库只保留53 MB处理后smoke示例；原始H5/H.265和全量归档索引
  已移到项目外私有目录，内部共享地址和设备ID已从公开文件中移除。

## 外部阻塞

- 还没有下载约 11.66 GB base，因此暂时不能运行官方 inference、真实
  forward/backward 或 checkpoint round-trip。
- 两个 tar.gz 应先复制到服务器本地 NVMe，再做一次顺序索引；不应在 SMB
  上反复流式扫描 50 GB/216 GB 压缩 tar。
- 正式训练服务器、G2 SDK 合同、服务器到机器人网络和 Thor 型号尚未确定。

## 下一检查点

1. 服务器确定后复制全量归档并形成最终数据 manifest。
2. base 权重优先下载到正式服务器；只有确认需要本地真实推理时，才在
   4090D 下载或复制同一份 base。
3. 有 base 权重后运行官方 demo inference、真实 forward/backward、
   checkpoint round-trip 和短程 LoRA。
