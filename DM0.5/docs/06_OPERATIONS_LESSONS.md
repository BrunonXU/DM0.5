# DM0.5 × G2 经验与踩坑记录

更新时间：2026-07-28

## 1. 文档职责

本文记录已经实际遇到、定位并验证过的问题。它不是当前状态页，也不重复冻结
合同。新增经验必须写清现象、原因、有效处理和禁止做法。

## 2. 文档与状态管理

### 经验：长期事实和实时状态必须分开

早期01、02、07、08都保存了相似的合同和迁移状态，服务器条件变化后很快
互相冲突。

当前规则：

- 01只保存目标与路线；
- 02是唯一动态状态；
- 03只保存数据证据；
- 04是唯一训练与部署合同；
- 05只回答相对上游改了什么；
- 06记录经验；
- 07是执行交接；
- 08只保存服务器实测资产和事件。

不要把实时百分比复制到多份文档，也不要把聊天记录当权威状态。

## 3. Git与私有仓库

### 现象

私有仓库直接`git clone`失败，看起来像仓库不存在或无权限。

### 原因与处理

私有仓库需要当前服务器账号完成GitHub认证。先确认remote和工作树，再使用
用户明确授权的认证方式。认证完成后记录实际HEAD，不根据交接文档猜commit。

### 禁止

- 不把token、密码或完整凭据写入Git、文档或命令日志；
- 不因clone失败就改成公开仓库；
- 不对已有工作树执行`git reset --hard`。

## 4. 项目目录与NVMe

### 经验

根盘剩余空间不足以容纳原始归档、模型和checkpoint。选定的3.5TB NVMe通过
bind mount放到`DM0.5/server/`，兼顾大文件物理隔离和“项目是最小单位”的
管理要求。

### 规则

- 所有训练相关大文件从`server/`访问；
- `server/`被Git忽略；
- NVMe各目录必须有README模板；
- 原始归档完成校验后设为只读；
- 不把大文件临时落到根盘或项目外的无说明目录。

## 5. Docker权限与GPU runtime

### 现象

宿主机`nvidia-smi`正常，但普通用户无法访问Docker socket。

### 原因与处理

Docker socket属于`root:docker`且权限为`0660`。正确处理是把项目账号加入
`docker`组并重新建立登录会话，然后验证：

1. `docker version`；
2. NVIDIA runtime；
3. 容器内8卡可见性。

不需要因此重装驱动、Docker或Container Toolkit。

### FlashAttention

RTX 6000D的计算能力和FlashAttention支持应通过实际镜像验证。项目正确性
基线使用eager/SDPA；正式训练按照上游组合启用FlashAttention，并在基线通过后单独
构建。不要把可选内核编译当成开工前置条件。

## 6. SMB挂载

### 现象

桌面文件管理器能够访问共享盘，但服务器shell看不到GVFS路径；直接CIFS挂载
又出现`permission denied`。

### 原因与处理

GVFS是用户会话挂载，不等于服务器全局挂载。CIFS认证需要正确区分用户名和
域；本环境使用工号作为username、`huaqin.com`作为domain。挂载成功后保持
只读，并先核对三个源文件的精确字节数。

### 安全

- 密码交给交互式提示或安全凭据文件，不写入仓库；
- 共享盘只作为源，不向其写转换产物；
- 不直接从SMB训练；
- 活跃复制期间不重新挂载CIFS。

## 7. SMB大文件复制提速

### 现场证据

- 实际链路是1GbE；
- CIFS使用SMB 3.0和4MiB `rsize/wsize`；
- NVMe利用率接近0%，瓶颈不在目标盘；
- 单个`rsync`长期约20～25MB/s；
- 两个不同文件并行时聚合约31.5MB/s，提高约30%～50%；
- 三流短测约28.5～29.2MB/s，没有继续提升。

### 有效策略

1. 每个归档只允许一个写进程；
2. 使用`--partial --append-verify`保留断点；
3. 从两个不同归档并发开始；
4. 用目标文件20秒增量测聚合吞吐；
5. 并发增加但聚合速度不升时立即停止扩容；
6. 完成后核对精确字节数并设为只读。

### 禁止

- 不让多个`rsync`同时写同一目标文件；
- 不把单个压缩包随意切成多个普通rsync区间；
- 不用“进度到100%”替代最终字节数和归档可读性检查；
- 不为了校验而对远端大文件反复做全量读取。

## 8. 压缩归档处理

### 经验

对50GB和216GB的tar.gz，如果每个episode都重新从头扫描，会反复解压整个
前缀，复杂度和SMB读取量不可接受。

正确做法：

- 先复制到本地NVMe；
- 使用`r|gz`单遍流式读取；
- 只解析metadata和必需成员存在性；
- 报告先写临时文件，再原子替换；
- 全量索引通过后再规划一次性提取训练必需字段；
- 不解压雷达、深度、日志和额外相机。

ZIP有中央目录，可随机读取metadata；tar.gz与ZIP不能套用同一种访问策略。

## 9. 视频与H5对齐

### 踩坑

H5顶层帧号看似从0连续，但不能直接作为原始视频帧号。真实样本中头部和腕部
视频的起始偏移不同，腕部还有较多重复画面。

### 正确处理

每一路相机必须读取`.txt`时间戳，用H5的`timestamp/camera/*`做精确查表。
缺失或无法匹配时直接失败，不静默退化为H5帧号索引。

## 10. 32D到34D模型适配

### 踩坑

如果直接用34D配置加载官方32D base，动作投影可能整体重建，导致原32维预训练
权重丢失；数据padding或推理路径也可能静默截断最后两维。

### 正确处理

1. 按官方32D形状加载base；
2. 保留原32维并扩展到34D；
3. 标准初始化新增两维；
4. 再加载LoRA adapter；
5. 对训练、保存、恢复和推理统一检查34D；
6. 用梯度测试确认第33/34维不是恒零。

## 11. GPU“空闲”不等于可以正式训练

GPU释放只满足资源条件。正式训练还依赖：

- 可复现镜像；
- 校验完成的base；
- 冻结的数据manifest、split和norm stats；
- 单卡真实forward/backward；
- checkpoint round-trip；
- 500～1000 step多卡基准。

等待大文件期间可以并行完成脚本、索引、测试和配置审查，但不能跳过门槛直接
用未冻结数据训练。

## 12. 长任务不能绑定IDE终端

### 踩坑

最初的base下载和两个`rsync`由Codex/VS Code终端子进程运行。切换工作区后
终端生命周期结束，三个进程收到SIGTERM；断点文件仍在，但任务停止。

### 正确处理

服务器支持用户级systemd。长任务使用临时user service管理，并配置
`Restart=on-failure`，例如：

```bash
systemd-run --user \
  --unit=dm05-base \
  --collect \
  --property=Restart=on-failure \
  --property=RestartSec=5s \
  COMMAND
```

状态和日志分别通过`systemctl --user status UNIT`与
`journalctl --user -u UNIT`查看。这样关闭VS Code或Codex会话不会终止任务。
使用`rsync --append-verify`恢复时会先核对已有断点，短时间文件大小不增长
不代表卡死。

## 13. 官方镜像不等于所有官方内核都适配当前GPU

### 13.1 三段attention分别是什么

DM0.5 recipe有三处独立的attention backend，书写顺序固定为：

```text
LLM / Vision / Action Expert
```

官方镜像中上游`DM05ModelConfig`的默认值实测为：

```text
FlexAttention / FlashAttention2 / SDPA
```

服务器正式采用：

```text
SDPA / FlashAttention2 / SDPA
```

因此只替换第一段LLM backend：`FlexAttention -> SDPA`。视觉塔仍使用官方
FlashAttention2，Action Expert仍使用官方SDPA。

### 13.2 为什么不能原样使用官方默认

官方默认组合在RTX 6000D上可以完成forward，但在LLM FlexAttention backward
编译阶段失败：

```text
required shared memory: 114,688 bytes
RTX 6000D hardware limit: 101,376 bytes
```

这是单个Triton kernel的片上shared-memory资源超限，不是85GB显存不足。
增加GPU数量、减少batch size或释放其他卡都不会提高单个SM的硬件上限。

该错误已在固定digest的官方镜像
`sha256:60a754dce047fbef40ab7d3a160fd82ccfbf5fa0509d862b6702311499dfbd4d`
中原样复现；自建FA2镜像也得到相同错误。因此不能把原因归到项目自编译的
FlashAttention wheel，也不能通过“换回官方Docker”直接解决Flex backward。

### 13.3 替换了什么、没有替换什么

替换内容：

- LLM attention的执行内核从PyTorch FlexAttention/Triton改为PyTorch SDPA；
- 服务器launcher显式传入`--model-config.llm-attn-implementation sdpa`。

保持不变：

- 官方Docker环境、PyTorch、CUDA、Transformers和Triton版本；
- DM0.5 base权重和模型层数、hidden size、tokenizer；
- 视觉编码器的FlashAttention2；
- Action Expert的SDPA；
- 34D G2动作投影、50步action chunk和三路RGB输入；
- LoRA target、rank、alpha、优化器、loss和checkpoint格式；
- 数据内容、normalization和训练目标。

这不是把整个模型退回eager，也不是移除FlashAttention。实际仍然使用视觉FA2，
只绕过无法在当前GPU上生成的LLM Flex backward kernel。

### 13.4 对训练的影响

**模型语义与质量：** 对当前LLM的dense/causal attention用法，FlexAttention
和SDPA实现相同的缩放点积attention语义。替换不会改变参数形状、训练目标或
checkpoint合同；不同kernel会产生正常的浮点舍入差异，因此不能宣称逐bit一致。
没有证据表明SDPA会系统性降低策略质量，最终仍必须用固定validation split和
离线动作指标验证。

**速度：** Flex在能够编译的硬件上可能有更好的融合或mask定制性能；在本机它
无法backward，实际可用吞吐为零。SDPA的最终1/4/8卡吞吐需要通过500～1000 step
基准测量，不能从单步smoke外推正式训练时间。

**显存：** backend的中间张量和调度不同，显存可能变化。官方镜像下
`SDPA / FA2 / SDPA`的单卡真实LoRA backward峰值为13.278GiB，显存不是当前
瓶颈。

**复现：** 正式launcher固定官方镜像digest并显式记录这一个覆盖，避免
`latest`漂移，也避免后来把兼容配置误认为官方默认。自建FA2镜像只保留作回滚，
不作为正式训练入口。

官方训练镜像不包含`h5py`，因此不能承担原始H5审计。数据审计/视频转换使用
项目数据工具镜像`opendm:g2-34d-fa2`，正式模型训练仍使用固定digest的官方
镜像。预处理容器和训练容器职责分离，不等于正式训练退回自建环境。

### 13.5 验收证据与决策

- 官方默认Flex backward失败日志：
  `server/runs/smoke_1ep/official_image_flex_backward.log`；
- 官方镜像兼容组合backward通过日志：
  `server/runs/smoke_1ep/official_image_server_compat_backward.log`；
- 官方镜像从checkpoint-22恢复并续训到24步：
  `server/runs/smoke_1ep/official_image_resume_22_to_24.log`。

所以当前决策是：第一次正式训练使用官方镜像和官方训练实现，只做
`LLM Flex -> SDPA`这一项硬件兼容覆盖。这比自行修改Flex Triton block size、
`num_stages`或kernel源码更接近官方、风险更小。

只有满足以下任一条件才切回Flex：

1. 上游发布并验证适配RTX 6000D/sm120的Flex kernel配置；
2. 更换的训练GPU能够通过同一真实LoRA backward测试；
3. 项目自行修改Flex kernel后，完成数值对照、吞吐、显存、checkpoint恢复和
   validation回归。

## 14. 短训必须验证完整恢复链路

“日志跑到最后一步”不足以证明训练环境完整。最小闭环应包含：

1. checkpoint内adapter、norm stats、优化器、调度器和RNG状态检查；
2. 用全新进程加载adapter并合并到base；
3. 对固定真实样本和固定flow噪声做base/adapter对照；
4. 从最后一个checkpoint恢复并实际多跑至少一步。

本次单episode实验还暴露出两个工程问题：

- G2入口原先调用不存在的`run()`，应按上游任务模式分派`train()`和
  `inference()`；
- checkpoint校验器不能要求LoRA目录一定有完整模型的`config.json`，应接受
  `adapter_config.json`并继续检查34D norm stats与必要密集模块。

短训loss下降只能证明短程拟合和数据链路有效，不能当作泛化或真机成功率。

## 15. Validation必须复用train-only norm stats

### 踩坑

上游norm stats文件名digest包含`dataset_name`。把评估数据集切到
`g2_sort_validation`后，默认会寻找一份validation专属统计文件。为了让程序
通过而计算这份文件会造成验证集统计泄漏，也会使训练与验证处于不同缩放空间。

### 正确处理

- 数据内容由`g2_sort_validation`选择；
- normalization路径强制指向冻结的`g2_sort_train`统计；
- 不生成validation norm stats；
- 固定样本、固定flow seed同时用于base和adapter。

本次评估器第一次因此明确失败，没有静默回退；修正后base与checkpoint-500
使用同一train-only统计重新运行。

## 16. 多卡短基准必须核对global batch合同

第一次8卡500-step链路预热使用per-device batch 1，global batch实际为8。
它能够证明多卡链路可运行，但不能替代冻结合同中的global batch 32吞吐和显存
门禁。

正式门禁重新使用：

```text
8卡 × per-device batch 4 × accumulation 1 = global batch 32
```

实测500步用时806.8秒、19.83 samples/s、每卡峰值约19.54GiB。报告实验时必须
同时写GPU数、per-device batch、accumulation和global batch，不能只写“8卡”。

## 17. 断点恢复后核对参数dtype

官方Trainer从完整checkpoint恢复训练成功，但本次实测恢复后的PEFT保存会将
104,848,384个LoRA参数由BF16提升为FP32。参数键和总参数数没有变化，恢复训练
也正常，但adapter及optimizer文件会变大。

因此每次正式服务重启后应记录：

- 实际恢复的checkpoint和global step；
- adapter键数、参数量和dtype分布；
- 首批loss、gradient norm和显存；
- 新checkpoint文件大小。

这不是漏权重，也不能因文件变大误判为模型结构改变；但为了可复现，不能隐去
恢复前后的dtype差异。

## 18. 停止launcher不等于Docker容器已经停止

从用户级systemd迁移到tmux时，停止外层service后，Docker daemon中的旧
`docker run`容器仍短暂存活。此时启动新容器会出现每卡两套训练进程、显存
接近翻倍，并可能并发写同一output和日志目录。

迁移长训练必须按以下顺序：

1. 停止外层systemd或tmux launcher；
2. 用`docker ps`核对实际训练容器，不能只看外层service；
3. 用`nvidia-smi --query-compute-apps`确认GPU进程为0；
4. 若旧容器仍在，按精确container ID停止；
5. 将未形成checkpoint的尝试日志单独归档；
6. 只启动一个新容器，再同时核对tmux、Docker数量和每卡进程数。

本次两个重叠尝试都未到首个checkpoint，已停止并把混合日志保存为
`server/runs/formal_50k/migration_overlap_attempt.log`。正式日志随后从step 1
干净重启。

## 19. tmux误杀后先查Docker，禁止直接重启训练

`docker run`客户端、`tee`和tmux可以退出，而Docker daemon中的容器继续运行。
此时文件日志看似停止，不等于训练停止。若直接从checkpoint启动，会再次形成
两套8卡训练。

正确恢复顺序：

1. `docker ps`确认训练容器；
2. `nvidia-smi --query-compute-apps`确认rank PID；
3. `docker logs --tail`确认实际step继续增长；
4. 从最后一个文件日志step之后回填Docker日志；
5. 把新tmux只作为`docker logs -f`查看器；
6. 独立guardian用`docker wait`保留`--rm`容器退出码，只有非零退出才恢复。

本次原文件日志结束于31,759，Docker已继续到31,800以上。以step 31,760为边界
补齐后抽查相邻step均各出现一次，训练没有回滚或改变参数dtype。
