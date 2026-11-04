# H20 积木任务环境部署与运行验收

日期：2026-10-15（北京时间）。执行账号 `user`，由 6000D 经 `h20-train` SSH6622 访问 H20；未安装 Codex、未使用 sudo、未修改同事环境。当前使用入口是 [ENVIRONMENT.md](../../ENVIRONMENT.md)，本页保存本次实验记录。

## 结果

| 验收 | 结果 / 证据 |
| --- | --- |
| 训练环境 260 包 | pip check、实际 RLinf/OpenPI worker/FSDP 模块导入、包来源及用户 site 隔离通过；[runtime-v21.json](runtime-v21.json) |
| 数据环境 111 包 | pip check、LeRobot v3 读写与包隔离通过；[runtime-v3.json](runtime-v3.json) |
| 视频链路 | 两环境 PyAV/libx264 编码12帧 → TorchCodec CPU 解码通过；PyAV与TorchCodec抽样输出一致；不等同于实际F1视频数据验收 |
| 数据语义 | 各自产生3个episode×60帧，state16/action16；H50完整窗口、尾部padding通过；v3额外检查非连续episode `[0,2]` 子集 |
| 官方模型 | [原件与CPU转换记录](../20261015-base-model/README.md)：全部29文件校验、667 tensors 键和形状/有限值验证通过 |
| 实际 tokenizer | 禁止 socket.connect 后调用 PaligemmaTokenizer，通过缓存产生200-token数组，149个有效token；[tokenizer-smoke.json](tokenizer-smoke.json) |
| 单卡模型 | 真实π0.5 base 的合成输入 SFT loss、反向、输出头更新及小型checkpoint恢复通过；[model-smoke.json](model-smoke.json) |
| 日常激活与资源释放 | 两套激活脚本检查通过，所用 GPU 已无计算进程；[final-state.json](final-state.json) |

CPU运行验收分别耗时48.34秒和9.47秒，未启动Ray服务或初始化CUDA。合成LeRobot数据仅检查数值特征，视频解码单独检查，不将两者拼称“真实F1数据读通”。临时合成数据已清理。

## 单卡模型验收的范围

- 测试前采样选择 GPU7：`GPU-023683b6-ff05-eada-62a0-fa4cff1c49a7`，当时无计算进程，空闲143167MiB；选择证据见 [gpu-selection.json](gpu-selection.json)。这不是持久预留卡号。
- 使用已核验的 `pi05-base-e1b61876289b52fa-openpi-rlinf-fp32`，3,353,433,872参数，H50、内部32D、token200。
- 合成三路224×224图像（左腕mask关闭）、state32、token IDs、H50×32目标；FP32参数、bf16 autocast、梯度检查点。
- SFT loss `0.0042999302968382835`；662个梯度tensor可检查，其中661个非零且全部有限。
- 对 `action_out_proj` 做小型 SGD 更新，保存和恢复该头及其优化器状态；临时checkpoint133437bytes，往返通过后删除。**没有运行全参数优化器更新或保存完整训练checkpoint。**
- 峰值 allocated 显存 **28.12746GiB**，reserved28.61914GiB；前向/反向/检查约11.03秒，全流程含hash/import约94.5秒。
- 收尾所用GPU显存报告0MiB、无计算进程；没有常驻训练任务。

测试实现：[smoke_runtime.py](smoke_runtime.py)、[smoke_model.py](smoke_model.py)。JSON记录脚本SHA256、模型SHA256及边界条件，不能在更改脚本或包版本后沿用旧PASS。

## 输入身份与安装选择

[bootstrap-inputs.json](bootstrap-inputs.json) 固定 uv wheel、独立Python和RLinf源码快照来源。最终包清单为 [训练 freeze](train-pip-freeze.txt)、[数据 freeze](data-pip-freeze.txt)；完整带哈希锁位于 [训练环境](../../envs/f1-jimu-pi05-v1/requirements.lock) 与 [数据环境](../../envs/f1-jimu-data-v3-v1/requirements.lock)。

本次实际解决的依赖问题：

1. OpenPI `numpy<2` 与 LeRobot0.4.x→rerun-sdk的NumPy2要求冲突，拆成训练与数据两个环境；未忽略依赖约束。
2. RLinf使用 `rlinf-transformer-openpi` fork，不能再混装stock transformers。LeRobot0.3.3留在训练环境；v3数据使用LeRobot0.4.3，后续需要F1消费适配层。
3. PyAV14.3/14.4缺少本次Python/Linux组合的适用wheel，训练选择14.2.0，避免临时全局安装编译依赖。
4. PyTorch cu128索引指向的 `download-r2.pytorch.org` 被公司代理拒绝，换用同文件的 `download.pytorch.org` 官方URL与官方SHA256，完整下载及安装通过。
5. 使用官方Ubuntu deb解包的用户态FFmpeg，解决TorchCodec运行时共享库依赖；系统包数据库保持不变，见 [FFmpeg证据](../20261015-ffmpeg/README.md)。

网络采样与源选择见 [测速记录](../20261015-bootstrap-network/README.md)。H20采用公司代理+清华，普通wheel两次完整下载4.10–5.24MiB/s；6000D官方直连可作备用。大模型复用6000D经官方清单核验的缓存后直接rsync到H20，没有笔记本中转。

## 收尾容量

12:01北京时间采样：个人环境约16.63GiB、原始/转换模型约24.08GiB、缓存约10.18GiB，工具/独立Python/暂存约0.35GiB，合计约51.25GiB。缓存用于后续重建，不因安装完成就删；空间不代表未来配额。

`/`可用约77.23GiB，`/home`约556.95GiB，`/u01`约26.39TiB。这是共享盘即时快照；所有大模型、环境、下载缓存及临时文件明确位于本人 `/u01/user`。详细字节数在 final-state.json。

## 尚未验收

`training_ready=false`。人工批准示教、ROI、F1 reader/adapter、当前v3数据到OpenPI消费层的桥接、动作起点筛选、16D↔32D数值往返、训练集norm_stats、真实batch均属于任务后续工作。OpenPI0.1.1任务prompt变换假设tasks为dict，v3实际为DataFrame，不能直接混用。

多卡FSDP/NCCL、全参数优化器内存、真实数据吞吐、正式训练及真机表现未由本次单卡合成测试证明。环境搭建完成后不自动占用全部8张卡；训练前复查实时资源，并协调卡号和分布式端口。
