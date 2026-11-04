# π0.5 base 与 tokenizer 准备记录

日期：2026-10-15（Asia/Shanghai）。用途：为 F1 积木任务暂定 RLinf–π0.5 recipe 准备可信、可追溯的基座模型；不启动训练和机器人。

## 来源与选择

- 基座：Physical Intelligence 官方 [`gs://openpi-assets/checkpoints/pi05_base`](https://github.com/Physical-Intelligence/openpi#model-checkpoints)，JAX/Orbax OCDBT 格式。
- 复用 6000D 已有 `/home/user/projects/PI0.5/checkpoints/pi05_base`；没有重新下载一套相同大模型，也没有用 LIBERO/BEHAVIOR 专用 checkpoint 代替 base。
- 29 个官方对象合计 **12,441,749,581 bytes**。所有对象的 CRC32C 与官方 GCS 当前清单一致；带 MD5 的对象同时核对 MD5，并为每个对象计算 SHA256。
- GCS 不是 Git 仓库：版本由每个对象的 `generation` 固定，规范化清单的 SHA256 为 `e1b61876289b52fac24e6877aaf9667a0ccfe3efabc52a710e1b4a132bf6223d`。目录名用此前 16 位。
- Tokenizer：官方 `gs://big_vision/paligemma_tokenizer.model`，generation `1711547605575873`，4,264,023 bytes；CRC32C/MD5/SHA256 已核验。
- Tokenizer SHA256：`8986bb4f423f07f8c7f70d0dbe3526fb2316056c17bae71b1ea975e77a168fc6`。

原始缓存只读，模型文件不进入 infra Git/文档镜像目录。6000D 新下载的小型 tokenizer 放在 `.artifacts/h20-bootstrap/models/`，大模型沿用旧缓存。

## H20 目标

| 内容 | H20 路径 |
| --- | --- |
| 官方原始 base | `/u01/user/models/pi05-base-e1b61876289b52fa` |
| 原始 base 来源 | 上述目录的 `SOURCE_MANIFEST.json` |
| Tokenizer | `/u01/user/models/tokenizer-1711547605575873/paligemma_tokenizer.model` |
| 已转换的训练基座权重 | `/u01/user/models/pi05-base-e1b61876289b52fa-openpi-rlinf-fp32` |

H20 原始 base 和 tokenizer 已实际完整接收、逐文件 SHA256 校验通过，见 [base 验收](h20-base-verification.json) 与 [tokenizer 验收](h20-tokenizer-verification.json)。三流续传阶段耗时 282.37 秒；该数值不含前面的单流及最终远端校验时间。

CPU 转换也已完成，见 [conversion-manifest.json](conversion-manifest.json) 及 H20 新模型目录 `CONVERSION_MANIFEST.json`。输出 **667 个 tensor、3,353,433,872 个参数，全部 fp32 且有限**，与裸 Pi0 meta 模型全部键/形状一致；JAX 设备为 CPU，无 GPU 使用。

| 输出文件 | 字节数 | SHA256 |
| --- | ---: | --- |
| `model.safetensors` | 13,413,809,816 | `f6391204c480d6c54d31d63601e7963ef23301157df052a4b8183e7b019ce2f1` |
| `config.json` | 202 | `1c85bc28303a2144a1cc3af492b1b6d91f9cdcd94b30f04212505ffb5118f1b0` |

转换不包含 F1 norm_stats，`training_ready=false`；完整模型权重可用不等于 F1 reader/适配器及训练配置已验收。

数据经 6000D→H20 直连 SSH 6622/rsync 传输，不经过笔记本。`stage_cached_base.py` 只传清单中的 29 个文件，支持部分文件续传，完成后在 H20 逐文件计算 SHA256。首次单流观测约 7 MB/s；为缩短等待，仅中断自身 rsync 并保留 partial，改为三组互不重叠文件并发续传，不并发写同一文件。最终 receipt 的 `transfer_elapsed_seconds` 记录三流续传这次执行时间，不包含之前单流时间。

## RLinf 格式转换

暂定路径为 `model_type: openpi_rlinf`，需要键名为 `img.*`、`llm.*`、`action_in_proj.*` 等的裸 Pi0 `model.safetensors`。原始 Orbax 目录不能直接冒充该格式。

RLinf `d00fe72060a5aa1554e92fe7bf72846d43d48c1c` 自带 JAX→OpenPI_RLinf 转换数学；其 CLI 同时强制复制一个 norm_stats 文件。F1 尚未计算训练划分统计，因此 [convert_base_only.py](convert_base_only.py) 直接复用同一模块的加载/转换函数，只生成基座权重及配置，**不从其他机器人借用统计**。

本次已执行的完整 CPU 命令（在 H20 个人环境执行；输出已存在，脚本会拒绝覆盖）：

```bash
/u01/user/envs/f1-jimu-pi05-v1/bin/python \
  /home/user/projects/infra/h20/experiments/20261015-base-model/convert_base_only.py \
  --rlinf-root /home/user/projects/vendor/RLinf-d00fe72060a5 \
  --source-provenance /home/user/projects/infra/h20/experiments/20261015-bootstrap/bootstrap-inputs.json \
  --input /u01/user/models/pi05-base-e1b61876289b52fa \
  --output /u01/user/models/pi05-base-e1b61876289b52fa-openpi-rlinf-fp32
```

脚本固定 `CUDA_VISIBLE_DEVICES=''`、`JAX_PLATFORMS=cpu` 和 4 个 PyTorch 线程；源文件校验、fp32/有限值验证、与 meta-device Pi0 模型的完整键/形状匹配通过后，才将 `.converting` 新目录重命名为最终目录。保留原始 base，不覆盖任何既有模型。

模型内部 action_dim=32、action_horizon=50、max_token_len=200。这不是 F1 外部动作合同：F1 保留 `f1_upper_16d_v1`，未来适配层需要补齐/裁回并验证数值往返；左腕 mask、ROI、F1 数据配置及 norm_stats 尚不能由本次模型准备代替。

Tokenizer 已另行校验复制到 `/u01/user/cache/openpi/big_vision/paligemma_tokenizer.model`，文件 `0600`，新建缓存父目录 `0700`；没有修改既有父目录权限和原始 tokenizer。使用 `OPENPI_DATA_HOME=/u01/user/cache/openpi` 即命中标准缓存，保持 SentencePiece/tokenizer 逻辑不变。具体文件校验见 [tokenizer-cache-verification.json](tokenizer-cache-verification.json)；另行禁用 socket.connect 后调用实际 PaligemmaTokenizer API 通过，输出200 tokens，见 [离线 tokenizer 验收](../20261015-bootstrap/tokenizer-smoke.json)。该项使用合成 prompt/state，不代表 F1 任务文本已定稿。

转换以独立 tmux 会话 `pi05-base-convert-20261015` 运行，完成后退出；日志位于 `/u01/user/runs/bootstrap-20261015/pi05-base-conversion/conversion.log`，状态文件为 `conversion.status`，结果 `PASS`。[run_conversion.sh](run_conversion.sh) 是本次完整启动脚本。

## 证据文件

- [gcs-objects.json](gcs-objects.json)：官方 base 对象的 generation、size、CRC32C/MD5 元数据。
- [base-manifest.json](base-manifest.json)：6000D 全文件校验结果和 SHA256。
- [h20-base-verification.json](h20-base-verification.json)：H20 29 文件全部 SHA256 验收通过。
- [tokenizer-object.json](tokenizer-object.json)、[tokenizer-manifest.json](tokenizer-manifest.json)：官方 tokenizer 元数据与下载校验。
- [h20-tokenizer-verification.json](h20-tokenizer-verification.json)：H20 tokenizer 的实际校验。
- [tokenizer-cache-verification.json](tokenizer-cache-verification.json)：OpenPI 标准缓存的内容和权限校验。
- [conversion-manifest.json](conversion-manifest.json)、[conversion.log](conversion.log)、[conversion.status](conversion.status)：真实 CPU 转换与全量权重验收。
- [verify_cached_base.py](verify_cached_base.py)、[stage_cached_base.py](stage_cached_base.py)、[convert_base_only.py](convert_base_only.py)：本次可重审的准备脚本。

CRC32C 校验工具为官方 PyPI `google-crc32c==1.7.1` cp310 manylinux x86_64 wheel，SHA256 `dcdf5a64adb747610140572ed18d011896e3b9ae5195f2514b7ff678c80f1603`，解压到 `/tmp/h20-model-check-tools`，没有改动系统 Python 包。
