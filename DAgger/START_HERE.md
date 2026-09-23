# 启动参考：当前因右拇指送修暂停

> **2026-09-10：本阶段暂停，G2右手拇指故障送修（操作者现场确认）。** 待维修后重新验收再恢复真机采集；已有数据保留，`training_ready=false`。详见 [暂停交接记录](doc/PAUSED_HAND_REPAIR_20260910.md)。

> 当前实现/证据/未决问题以 [当前进度](RECORDING_AND_PROGRESS.md) 为准；按序查阅 [文档索引](doc/00_INDEX.md)。POLICY 为单34D normal/100Hz；人工仍是臂Servo＋手5Hz点目标。多轮真实HIL已有证据，training_ready=false。

**G2工作目录：`/home/agi/xza/DAgger`。Windows工作目录：`C:\Users\100850113\DAgger`。**
当前已完成多轮真实交接；最近已审计/导出175457轮，视频227.634秒。图像时间对齐和训练标签尚未认证，不能直接用于训练。

> 首次START设计证据：[首次START开始、三轮数据和存储审计](doc/START_GATE_AND_THREE_RUNS_20260908.md)。连接不再自动运行；首次START开始POLICY，后续START切HIL。

## 更新 Windows 客户端（提示 v2 更新后需重新下载）

关闭旧 sender 后执行以下下载；仅更新提示无需重开已正常工作的 tunnel。PowerShell执行：

2026-09-07修复：此前候选sender CMD误写`--local port 7907`，现为`--host 127.0.0.1 --port 7907`。尚无Windows DAgger目录时必须先执行下面的scp，不能直接cd。普通遥操也可复用这套传输，G2运行`G2Teleop/scripts/12_g2_powered_f710.sh --port 7906`；START只在40入口切HIL。

```powershell
New-Item -ItemType Directory -Force "$HOME\DAgger" | Out-Null
scp -r agi@10.130.86.13:/home/agi/xza/DAgger/windows_client/src agi@10.130.86.13:/home/agi/xza/DAgger/windows_client/scripts "$HOME\DAgger"
```

沿用已验证的 `$HOME\.venv\Scripts\python.exe`，不新建环境。

> 2026-09-08满盘修复：G2的data/logs已完整校验迁移至`/data/logs/xza/DAgger/`，原路径为符号链接，启动命令不变。详见[存储修复](doc/STORAGE_FIX_20260908.md)。

## sender 提示怎么看（2026-09-08 v2）

- 等待开始 START：释放控制后单击 START 开始策略。
- 同步策略模式 DM0.5：G2 确认 POLICY（包括推理等待），START 请求人工介入。
- 人工介入 HIL：选侧并释放十字键，RB 控臂；松 RB 不退出。退出须松 RB、等待手部/Home 完成，再按 START。
- 等待新策略：正在等待新观测推理，正常等待即可。
- 安全暂停 SAFE_HOLD：RB/Y 不恢复，连接恢复后释放控制、按 START 重进人工。
- 连接异常：看带时间的错误及 session/ACK 序号；保持 tunnel，确认 G2 监听。具体原因见 [断线核对](doc/HIL_RECONNECT_RB_20260908.md)。

提示依据最后 G2 ACK，不保证实际运动。录制由 G2 recording-mode 决定。日志仍是 `logs/windows_sender_console.log`。

## 维修验收后恢复启动：三个窗口（当前仅保留参考）

用户约定（2026-09-08）：每次提供DAgger启动流程时，必须同时给出Windows隧道、Windows F710 sender、G2 Powered三个终端的完整命令（含cd），不能只发G2命令。

若预检显示 `G2 runtime occupied: PID ...`，先在该进程原终端退出它，再启动DAgger。2026-09-07现场PID2481159是仍存活的`g2_paos bridge --g2-read-only --operator-confirmed-exclusive`，不是残留锁；不要删`/tmp/g2_dm05_runtime.pid`绕过互斥。已修复为倒计时/加载GDK之前显示占用进程，正式启动仍保留原子抢锁。

Windows窗口①，SSH tunnel（本地7907→G2的7906）：

```powershell
cd $HOME\DAgger
.\scripts\windows_tunnel.cmd
```

Windows窗口②，F710 sender：

```powershell
cd $HOME\DAgger
.\scripts\windows_sender.cmd
```

第三个Windows PowerShell，先登录G2（停止其他Robot owner/12/19/旧40接收端）：

```powershell
ssh agi@10.130.86.13
```

看到agi@...的Linux提示符后再执行：

```bash
cd /home/agi/xza/DAgger
bash scripts/40_g2_powered_hil.sh --capture-hz 30 --recording-mode hil-record --duration-seconds 0
```

文件预检后按 **ENTER**，倒计时3秒；现场急停就绪。Windows连接后等待首次 **START**，收到新的观测和policy后产生真实动作。连接时先释放START，再单击。
上方命令用`--duration-seconds 0`取消自动结束，按Ctrl-C结束。不写该参数则默认首次START后120秒退出；退出后sender重连属预期。不要另起一个Robot进程同时遥操。

- 首次START开始POLICY；之后START进入人工，旧policy后缀作废。
- 十字键选侧并释放，RB控臂；松RB仍在HIL；RT控手；A切1/2倍速度；Y同owner Home。
- 松RB，等待手部渐进目标完成，再按START；先HOLD，基于新观测请求新policy后恢复。
- 退出被拒绝会打印理由。手部互锁目前检查命令生成完成，不是经过认证的物理到位测量。
- 网络恢复不会直接恢复policy：SAFE_HOLD时START先进人工，再START请求新policy。

## 录制模式：同一主链，只改变图像payload保存方式

默认（最近4秒预录，首次进入HIL后持续保存到结束（开始按钮后的下一次START））：

```bash
bash scripts/40_g2_powered_hil.sh --recording-mode prebuffer-record
```

短场景全程留存，方便查问题：

```bash
bash scripts/40_g2_powered_hil.sh --recording-mode hil-record
```

只推理/控制，不持久化原始图像payload；仍有journal/telemetry元数据，**不是完全不写盘**：

```bash
bash scripts/40_g2_powered_hil.sh --recording-mode inference-only
```

仅作文件/导入检查，不初始化GDK：

```bash
PYTHONPATH=src:/home/agi/xza/DM0.5:/home/agi/xza/DM0.5/g2_sort/deployment:/home/agi/xza/DAgger/vendor_ik python3 scripts/40_g2_powered_hil.py --preflight-files-only --accept-dm05-hand-fit
```

## 新日志和数据全部在 DAgger

- `data/raw/powered_hil/g2_powered_hil_<时间>/journal/`：观测、policy chunk、control、events。
- 同级 `payload/`：head/left_wrist/right_wrist原始图像、frames.jsonl索引、manifest。
- `logs/telemetry/powered/g2_powered_hil_*.jsonl`：主遥测；`logs/telemetry/feedback/`：反馈旁路。
- `logs/powered_hil_console.log`：追加的终端日志。
- `.partial`表示没有按完整数据发布，不能当作可训练记录。

数据保留与什么时候录制，详见根目录 [RECORDING_AND_PROGRESS.md](RECORDING_AND_PROGRESS.md)。
历史33–39脚本用于mock/审计/无输出诊断，**本次真实场景入口是40**。

## 固定生成三图状态回放（6000D执行，不是在Windows/G2）

固定脚本：`scripts/42_render_episode_preview.py`。依赖6000D现有Pillow、ffmpeg和Noto CJK字体；G2当前没有ffmpeg，因此录制在G2、视频制作在6000D。不在控制线程内编码视频。

录制结束后，把RUN_ID替换为G2打印的本次编号；以下为6000D Linux终端命令：

```bash
cd /home/xza_100850113/projects/DAgger
RUN_ID=g2_powered_hil_YYYYMMDD_HHMMSS
mkdir -p data/raw/powered_hil
scp -r "agi@10.130.86.13:/home/agi/xza/DAgger/data/raw/powered_hil/$RUN_ID" data/raw/powered_hil/
python3 scripts/42_render_episode_preview.py "data/raw/powered_hil/$RUN_ID" "data/exports/$RUN_ID/three_camera_preview.mp4"
ffmpeg -v error -i "data/exports/$RUN_ID/three_camera_preview.mp4" -f null -
```

只处理已结束且journal/payload完成发布的episode；若只有.partial，先核对终止原因。脚本拒绝覆盖已有MP4，重复渲染时改输出文件名。输出MP4、JSON状态时间段，以及超过30秒时生成的30秒JPEG预览。

流程：读取frames.jsonl和原图 → 校验源图大小/hash → 按host monotonic原速对齐三路 → 从事件/control_source标注DM0.5、HIL、Home、等待 → 30fps三图并排H.264视频。展示时沿用最近已到达图像，不虚构采集帧；不裁剪/加速等待。三路曝光同步与训练标签仍需单独审计。

新录制建议先用hil-record完整保存、首次START开始、--duration-seconds 0由Ctrl-C结束。它包含等待首次START的图像；训练数据门仍未通过，正式操作录制不等于可直接训练。
