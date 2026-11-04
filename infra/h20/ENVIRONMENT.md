# H20：F1 积木任务环境与 base 模型

更新：2026-10-15（北京时间）。本页维护本轮实际部署状态；原始硬件快照见 [系统档案](SYSTEM_PROFILE.md)，任务业务合同以 6000D 的 [训练 recipe](../../tasks/F1_ComplexyToys/training/README.md) 为准。

**6000D 放 Codex、开发和推理；H20 放数据、训练环境和 checkpoint。** 本轮只在 H20 的 `user` 个人目录部署，没有 sudo、系统 Python/驱动修改、全局代理或 shell 启动文件修改。

## 1. 两套环境已经安装

| 用途 | H20 解释器 | 关键版本 |
| --- | --- | --- |
| π0.5 / RLinf 训练准备 | `/u01/user/envs/f1-jimu-pi05-v1/bin/python` | Python 3.11.14、torch 2.7.1+cu128、torchvision 0.22.1+cu128、NumPy 1.26.4、RLinf 0.4.0、rlinf-openpi 0.1.1、LeRobot 0.3.3 |
| LeRobot v3 数据处理 | `/u01/user/envs/f1-jimu-data-v3-v1/bin/python` | Python 3.11.14、torch 2.7.1+cu128、NumPy 2.2.6、LeRobot 0.4.3、datasets 4.1.1、PyAV 15.1.0 |

训练环境 260 个包、数据环境 111 个包，均通过依赖一致性检查。两套环境的用户 site-packages 均关闭；实际模块路径核查没有混入系统或其他用户的包。环境安装采用 copy 模式，不依赖跨环境可写 hardlink。

**必须分开：** OpenPI 要求 `numpy<2`；LeRobot 0.4.x 的 `rerun-sdk>=0.24` 传递依赖要求 NumPy 2。只看 LeRobot 顶层 metadata 会遗漏这个冲突。没有用强制忽略依赖来制造一个表面可安装的混合环境。

训练环境的 LeRobot 0.3.3 只支持 v2.1；当前 F1 v3 候选由数据环境读取。两环境存在不等于已打通 F1 reader。后续需要明确的、经验收的数据适配层，不能直接把 v3 数据目录交给旧 loader，也不能悄悄降级原始数据。

锁定文件：

- 训练：[显式依赖](envs/f1-jimu-pi05-v1/requirements.in)、[完整带哈希锁](envs/f1-jimu-pi05-v1/requirements.lock)、[版本选择依据](envs/f1-jimu-pi05-v1/VERSION_DECISIONS.md)。
- 数据：[显式依赖](envs/f1-jimu-data-v3-v1/requirements.in)、[完整带哈希锁](envs/f1-jimu-data-v3-v1/requirements.lock)。
- 实际安装清单：[训练 freeze](experiments/20261015-bootstrap/train-pip-freeze.txt)、[数据 freeze](experiments/20261015-bootstrap/data-pip-freeze.txt)。

## 2. 代码、工具和存储布局

以下路径均在 H20：

| 路径 | 用途 |
| --- | --- |
| `/home/user/projects/vendor/RLinf-d00fe72060a5` | RLinf commit `d00fe72060a5aa1554e92fe7bf72846d43d48c1c` 的 git archive 快照；训练环境 editable 安装到此目录。不要原地覆盖正在运行的代码 |
| `/u01/user/python/cpython-3.11.14-linux-x86_64-gnu` | 独立解释器，未替换系统 Python |
| `/u01/user/tools/uv-0.13.1` | 固定环境管理工具 |
| `/u01/user/tools/ffmpeg-4.4.2-jammy` | 官方 Ubuntu deb 解包的用户态 FFmpeg 4.4.2 及共享库；没有系统 apt install |
| `/u01/user/cache` | uv、HF、OpenPI、编译缓存；编译缓存按环境标签区分 |
| `/u01/user/staging/tmp` | 当前任务临时文件，避免写满根盘 `/tmp` |
| `/u01/user/datasets` | 后续版本化数据集；本轮没有将所有采集数据搬入 |
| `/u01/user/runs/bootstrap-20261015` | 安装、环境验收及模型验收日志 |
| `/u01/user/runs/bootstrap-20261015-data` | 数据环境安装日志 |

训练环境已装 pip、pytest、CMake 3.31.10、Ninja、pybind11、nvitop、TensorBoard、Ray、Hydra、W&B/SwanLab 等。没有启动常驻 Ray 服务。2026-10-16起任务激活默认 W&B online；每次训练必须记录，连接方式见 [W&B监控](WANDB.md)。旧offline默认已替换。

唯一提供 `transformers` import 包的是 **rlinf-transformer-openpi 4.53.2**；未混装 stock transformers。不要在该环境另行 `pip install transformers`。两个 OpenCV distribution 是上游分别声明的依赖，训练环境均锁为 4.11.0.86；不要单独升级其中之一。

FFmpeg 使用单独用户前缀，90 个 deb 的哈希与依赖检查通过，系统包数据库未变化。激活只影响当前任务进程的 PATH/LD_LIBRARY_PATH；详见 [FFmpeg 验收](experiments/20261015-ffmpeg/README.md)。

## 3. 官方 base 与 tokenizer

| 资产 | H20 路径 | 身份与状态 |
| --- | --- | --- |
| 官方 π0.5 JAX/Orbax base | `/u01/user/models/pi05-base-e1b61876289b52fa` | 来源 `gs://openpi-assets/checkpoints/pi05_base`，29 文件，12,441,749,581 bytes；官方 CRC32C/可用 MD5 与双端 SHA256 通过 |
| RLinf 格式 base | `/u01/user/models/pi05-base-e1b61876289b52fa-openpi-rlinf-fp32` | 同一官方 base 在 CPU 转换；667 tensors、3,353,433,872 参数；全部 fp32 有限值，完整键和形状匹配 |
| tokenizer 原件 | `/u01/user/models/tokenizer-1711547605575873/paligemma_tokenizer.model` | 官方 GCS 对象 generation 固定，SHA256 校验通过 |
| tokenizer 实际缓存 | `/u01/user/cache/openpi/big_vision/paligemma_tokenizer.model` | 与原件相同；`OPENPI_DATA_HOME=/u01/user/cache/openpi` |

转换后 `model.safetensors` 为 13,413,809,816 bytes，SHA256：

```text
f6391204c480d6c54d31d63601e7963ef23301157df052a4b8183e7b019ce2f1
```

模型配置 H50、内部32D、token budget200、π0.5；F1 外部16D及正反变换仍由任务 adapter 实现。转换没有附带 BEHAVIOR/Franka/其他机器人的 norm_stats。原始 base 保留不变，转换源码、输入和输出身份见 [模型来源与转换记录](experiments/20261015-base-model/README.md)。

## 4. 实测下载渠道

同一固定 18,252,005-byte wheel，完整下载后对比官方 SHA256；测试仅代表本时段和样本。

| 发起位置 / 路线 | 结果 | 用法 |
| --- | --- | --- |
| H20 直接访问外部 PyPI/国内镜像 | 本轮均超时 | 不沿用“换个镜像必然能直连”的假设 |
| H20 公司代理 + 清华 | 两次完整下载 5.24 / 4.10 MiB/s | 普通依赖首选，安装过程已实际使用 |
| H20 公司代理 + 官方 PyPI 文件 | 20 秒内未完整下载 | 不用残缺样本做成功速度排名 |
| H20 公司代理 + 阿里/USTC/豆瓣 | 本轮 CONNECT 超时 | 当前不选 |
| 6000D 官方直连 | 4.17 MiB/s；清华2.76、USTC2.50 | 备用下载后 rsync，笔记本不参与 |
| H20 PyTorch CUDA wheel | `download-r2.pytorch.org` 被代理拒绝；`download.pytorch.org` 官方同文件直链完整安装成功 | 锁文件明确 URL + 官方哈希，不能只验证索引首页 |

公司代理为 `http://192.0.2.34:3128`，只在安装脚本当前进程设置；未写 `/etc`、用户 `.bashrc` 或全局 pip 配置。普通包源是 `https://pypi.tuna.tsinghua.edu.cn/simple`。GCS 官方模型元数据也可经公司代理访问；本轮大模型直接复用6000D已校验缓存，未重新下载第二份外网原件。

详细采样、限制与原始结果见 [网络测速记录](experiments/20261015-bootstrap-network/README.md)。更换来源时保留已固定的版本/哈希，不使用 `--trusted-host` 或关闭 TLS 校验。

## 5. 使用方法

先在 **6000D Bash** 进入 H20：

```bash
ssh h20-train
```

再在 **H20 个人 Bash** 中二选一；建议每个用途使用独立终端：

```bash
# 训练准备环境；GPU卡号与分布式端口须按当次任务协调，脚本不自动分配。
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
python -c 'import sys, torch; print(sys.executable); print(torch.__version__)'
```

```bash
# v3 数据环境：默认CPU，不占训练GPU。
source /home/user/projects/infra/h20/envs/f1-jimu-data-v3-v1/activate.sh
python -c 'import sys, importlib.metadata as m; print(sys.executable); print(m.version("lerobot"))'
```

激活脚本使用个人缓存、用户态 FFmpeg、关闭用户 site-packages；训练环境将 JAX 限制CPU，避免权重转换/变换预占训练GPU。激活不是全局持久设置，下一次新终端需要重新 source；不要将两个环境的 site-packages 混入 PYTHONPATH。数据环境会清空 `CUDA_VISIBLE_DEVICES`，同终端随后激活训练环境不会自动恢复；使用独立终端并显式选择当次协调好的 GPU。

依赖维护应在 6000D 修改本目录 requirements，重新解析和验收后再更新。[install_jimu_env.sh](scripts/install_jimu_env.sh) 是本轮安装阶段的历史脚本：假设 uv、解释器、venv、源码和日期 staging 已存在，运行会重新解析依赖、覆盖 staging lock/status 并修改既有环境；**不要把重跑它当成固定版本复现**。

锁定复现应新建环境，沿用仓库已有锁文件；以下为 H20 Bash 示例，只有需要重建时才执行，目录已存在会停止：

```bash
h20_rebuild=/u01/user/envs/f1-jimu-pi05-rebuild-v1
if [[ -e "$h20_rebuild" ]]; then
    echo '目标已存在；请选择新的版本目录。' >&2
else
    /u01/user/tools/uv-0.13.1 venv \
        --python /u01/user/python/cpython-3.11.14-linux-x86_64-gnu/bin/python3 \
        "$h20_rebuild" && \
    HTTPS_PROXY=http://192.0.2.34:3128 HTTP_PROXY=http://192.0.2.34:3128 \
    UV_CACHE_DIR=/u01/user/cache/uv TMPDIR=/u01/user/staging/tmp \
    /u01/user/tools/uv-0.13.1 pip install \
        --python "$h20_rebuild/bin/python" --no-config --link-mode copy --require-hashes \
        --default-index https://pypi.tuna.tsinghua.edu.cn/simple \
        -r /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/requirements.lock && \
    /u01/user/tools/uv-0.13.1 pip install \
        --python "$h20_rebuild/bin/python" --no-config --no-deps --no-build-isolation \
        -e /home/user/projects/vendor/RLinf-d00fe72060a5
fi
```

该示例未在现有环境上重跑；重建后需要重新验收、为新环境提供新的激活脚本。数据环境则使用自己的 lock，不安装 RLinf。

## 6. 验收与尚未完成的任务门槛

- 两环境 pip check、解释器及包来源隔离通过。
- 实际 RLinf SFT worker / FSDP 模块、OpenPI loader 导入通过。
- PyAV编码 → TorchCodec 解码通过；两环境合成 LeRobot 数据的 H50窗口和尾部 padding 回读通过；v3非连续episode子集检查通过。
- 官方 base、CPU权重转换、实际 tokenizer API 离线缓存调用通过。
- 单卡真实 base 的合成 SFT loss、全模型反向传播、输出头更新及小型 checkpoint 恢复通过；峰值 allocated 显存 28.13 GiB。不是全参数优化器 checkpoint 或完整训练验证，见 [本轮安装与运行验收](experiments/20261015-bootstrap/README.md)。
- 最终激活脚本通过，两环境、模型、缓存与工具共约 51.25 GiB；`/u01` 当时仍有约 26.39 TiB 可用。单卡测试已退出、所用 GPU 无计算进程。实时状态及容量分类见 [收尾记录](experiments/20261015-bootstrap/final-state.json)。

这些是基础设施验收，不是 F1 任务训练或真机成功。正式训练仍需：人工批准示教、ROI、F1 reader/adapter、v3到训练消费层的接口、动作起点筛选、16D↔32D数值往返、训练集归一化统计及真实 batch。OpenPI0.1.1 的任务prompt转换仍假设 tasks 是dict，而v3是DataFrame，必须在任务适配中处理。多卡FSDP/NCCL训练与实际吞吐尚未验收。

不得把其他机器人归一化统计补到base目录来绕过上述门槛；不得改写原始数据来伪装格式兼容。需要和同事协调共享GPU、内存、端口与盘容量；自己的环境不会阻止管理员改驱动或重启主机。

## 2026-10-27 F1 固定 writer 的独立 Python 3.12 环境

新 RLT 原件的 schema4 导出沿用 F1 固定 LeRobot writer；该源码已要求 Python 3.12，不能放进原 Python 3.11 数据环境。独立环境 `/u01/user/envs/f1-rlt-export-py312-v1` 已完成四项真实视频／有效性掩码／reader 回读，原训练和 v3 数据环境保持不变。

解释器为 `/u01/user/python/cpython-3.12.14-f1-export`，CPU torch 2.8.0 / torchvision 0.23.0；其余沿用 `DAgger/F1/configs/f1_export_requirements.txt`。固定源码暂存在 `/u01/user/staging/f1-rlt-local-20261027-r1/lerobot-pinned`，按 `f1_lerobot_binding.json` 校验，使用 `--no-deps -e`，不安装完整机器人依赖。

安装通过当前进程公司代理和官方 CPU wheel 直链完成；普通依赖使用已有清华源。未修改全局代理、系统解释器、GPU 环境或 shell 配置。完整版本、解释器 SHA 和测试日志保存在 `/u01/user/runs/f1-rlt-local-20261027-r1/export-env-{freeze.txt,receipt.json}` 与 `lerobot-py312-test.log`；[实现背景与验收](../../tasks/F1_ComplexyToys/inference/experiments/20261027-rlt-local-v2.md)。
