# 输入短暂停顿与录制解耦

日期：2026-10-15。实施当前状态与上线结果以 [STATUS](../STATUS.md) 为准。背景：[意外收尾](20261015-unexpected-episode-end.md)、[历史统计](20261015-input-history-analysis.md)、[Pico 对照](20261015-pico-input-comparison.md)。

## 原因和决定

原 `F1PolicyEngine.disconnect()` 对 watchdog、网络、目标故障均调用 `episodes.finish()`，只有相机故障标记 interrupted。因此短时输入超时既停动作又结束录制，并被正常完成音误导为 START。相机30Hz线程独立，输入间断本身并不要求结束整条记录；网络、USB和写盘各自的异常仍须区分。

保持30Hz、原始落盘合同、200ms输入watchdog、500ms sender ACK超时和原限位/碰撞/反馈保护。参考 meaxiao 的失鲜保持、清旧目标、释放后重新接管、采集独立和异常段保留；参考 LXL 的采集与遥操解耦。不照抄250/150/50ms参数、UDP或action=state回填，不声称有两套系统的可比单向延迟。

## 本次行为

- 已进入人工控制、同一连接/发送会话出现输入watchdog：进入 `input_wait_*`，底层保持 SAFE_HOLD，清Home/IK/输出轨迹/chunk；本条录制继续，既不新建条目，也不播放结束音。
- 操作者松 RB、摇杆回中、释放其他按钮，收到“按RB恢复”后再按 RB。使用暂停之后的新输入、真实反馈和新epoch恢复同一条；不增加新的介入身份。一直按RB、旧包、Y、RT均不能恢复或排队重放。
- 暂停中明确按 START，须输入新鲜且RB释放，结束当前示教条并保存；START不是暂停恢复捷径。保存完成后下次START开始下一条。
- 真实TCP断开、session变化、协议序列错误、相机/反馈/碰撞/发布故障仍走独立保护，不能用RB绕过。可提交的示教前缀标记 interrupted，需要人工审核，不播放正常完成音。磁盘失败仍报告保存失败。退出进程保存剩余条也不冒充START正常完成。
- 原Y路线及速度保持v30。网页仅增加既有状态组件的文字/黄色等待态，不调整布局。

## 如何防止TCP积压包恢复运动

F1只在暂停时给ACK附加随机 challenge，保存128项有界历史。新版Windows sender先读取ACK，再采样物理F710，把当时challenge和**原始手柄中立状态**一起写入该包。不能用RB门控后被清零的axes推断摇杆已回中，也不能给排队旧包补上新challenge。

F1用自己的单调时钟核验challenge归属、签发时刻与200ms预算；恢复RB的challenge还必须晚于已确认中立。无需两机时钟同步。失败的RB边沿被消费，即使继续按着RB等网络恢复也不启动，必须再释放/按下。机器人反馈沿用原profile新鲜度检查，并要求真实接收时间晚于暂停。

该证明是**保守的往返上界**，不是测得的单向延迟，仅用于恢复握手，不把普通运行的200ms接收watchdog改为持续RTT门控。链路往返超过200ms时继续保持/录制，等待可证实的新输入。常规运行仍不能凭ACK证明手眼端到端时延为200ms以内。

旧sender不具备原始中立状态/challenge回传能力；正常协议保持兼容，但暂停后会提示更新sender，不能伪造证明放行RB。部署本功能须同步更新Windows客户端；隧道参数不变。初次Enter/START和硬故障的原授权规则保留。

## 数据与计时

原始events增加 `input_pause_started`（含最后可信接收时刻）、`input_recovery_neutral`、`input_pause_ended`、`input_pause_escalated`。导出将最后可信接收→恢复之间视为保守无效区间，包含watchdog发现前不确定尾部；未恢复则排除尾部。观测、跨界相机帧、action和重采样不能跨此区间；epoch和真实回调连续性检查仍生效。正常人工保持不因动作小而删去。每个连续有效段单独作为训练episode；原件不改写，是否任务成功仍需审核。

Windows每5秒给出采样间隔、publish→发送worker、socket write、ACK RTT，各保留最多500样本。F1状态快照给出接收间隔与接收→控制消费统计，亦最多500样本。两机wall clock不作减法。没有声称这些统计覆盖电机跟随或镜头到屏幕延迟；未新增主动延迟/负载测试。

## 验证与边界

新增无动作测试覆盖：旧协议省略扩展字段；challenge跨session/过期/暂停前拒绝；真实回环协商；原始手柄非中立；一直按RB；失败边沿不能延后复活；新包不能覆盖历史间断；新反馈门控；Home/RT不重放；START结束暂停条；连续两条真实文件提交和声音次数；硬故障升级；导出切除不确定尾部且窗口不跨界。

开发机定向回归150 passed、3 skipped（缺可选正式LeRobot依赖）；首次全F1发现两项旧测试仍期待watchdog原因串，已更新为新暂停状态并保留零动作断言。另三项配置绑定测试因开发机F1-infra不是现场锁定版本失败，必须在目标ARM锁定依赖上检查，不能放松哈希。

ARM、发布哈希和现场操作验收结果补入STATUS；离线通过不代表实机长期稳定，也不解决之前已记录的Wi-Fi/USB/电机内部通信问题。


## 部署与操作

v31机器人端和4242状态提示已部署，ARM主回归191 passed / 3 skipped，最终受影响回归91 passed / 3 skipped；最终开发机97 passed / 3 skipped，浏览器5种状态通过。跳过项是锁定LeRobot环境用例，ARM数值/软件验证不代表现场动作通过。精确哈希和现役状态见[STATUS](../STATUS.md)。

Windows sender需要更新；未通过工具远程执行Windows程序。先正常保存当前条、关闭sender（Ctrl+C），在 **Windows PowerShell，任意工作目录** 执行：

```powershell
scp -F NUL user@192.0.2.3:/home/user/Bruno/DAgger/updates/20261015-input-pause-v31-r2/update_input_pause_r2.ps1 "$env:TEMP\update_f1_input_pause_r2.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\update_f1_input_pause_r2.ps1"
```

两行依次执行，第一行下载成功后再执行第二行；不要复制终端的 `>>` 提示符。r2更新两个sender入口和全部7个 `dagger_core` 模块，自动创建缺失目录，安装前后用独立Python进程加载sender（不初始化手柄、不联网、不发动作）。保留CMD、隧道、日志及Python环境；覆盖文件先备份，失败逐项回退并明确报告回退是否完成。目标目录可用 `-ClientRoot`，SSH地址可用 `-RobotHost`，解释器按 `F1_SENDER_PYTHON` 或 `$HOME/.venv/Scripts/python.exe` 解析，也可用 `-PythonExe` 指定现有sender解释器。更新包SHA256 `8b1c96ee2e84be5f37b1599bf761f487fd2de2cd919b22eee27d383f4c4420f9`。只有看到“更新成功：Windows sender v31-r2”才算完成；Windows实机执行仍待用户重试。

随后沿用三端（已运行的隧道无需重复开）：

1. **Windows PowerShell A**：`cd "$HOME/DAgger-F1"`，执行 `cmd /c scripts\windows_f1_tunnel.cmd`。沿用已核验的 `F1_SSH_HOST`；不要新开第二条占用7907的隧道。
2. **F1 现场终端**：`cd /home/user/Bruno`，执行 `bash /home/user/Bruno/DAgger/start_f1_hil_route.sh demonstration`；确认现场条件后Enter。日志 `/home/user/Bruno/DAgger-runtime/f1-09/onsite-powered-console.log`。
3. **Windows PowerShell B**：`cd "$HOME/DAgger-F1"`，执行 `cmd /c scripts\windows_f1_sender.cmd`。日志 `$HOME/DAgger-F1/logs/windows_sender_console.log`。

现场先验证一条短示教：START开始→选臂/RB→松RB→START结束→听保存完成音→下一次START新条。出现黄色输入暂停时不要按START恢复：松RB、回中，等“按RB恢复”再按RB；看到红色硬故障则按对应原因处理。不要通过拔网线或让机器人持续运动来制造中断。软件故障注入已在无动作回环和假输出测试中完成；真实暂停可在后续自然发生时对照记录。

训练导出使用新release中的 `f1dagger.export`（或统一适配器传入该release作为 `dagger_root`）；后台 `f1-conversion-scan.service` 当前仅扫描排队，没有自动重跑训练导出。已有v3派生数据不被原地重写。

空闲时机器人代码回退脚本位于 `/home/user/Bruno/DAgger-runtime/deployment-backups/20261015-input-pause-v31/rollback.py`，执行前校验当前哈希、检查活动控制并取得共享锁；只恢复v30入口及原网页提示，不删数据、不动机器人。Windows旧sender保存在客户端 `backups/input-pause-时间/`。

## 2026-10-15 Windows 安装失败与 r2 补证

用户首先在SCP下载前执行了PowerShell，提示脚本不存在；随后下载成功并运行原脚本，ZIP传输完成，但 `Copy-Item` 在 `src/dagger_core/protocol.py` 报父目录不存在。原安装器没有创建目标父目录，5文件增量包也遗漏了旧客户端缺少的其他公共模块。仅补mkdir不足以保证sender启动。用户输出未证明回退全过程成功，故未宣称Windows已更新。原ZIP SHA256 `269e854230060091b88f2c7f5881d5e9dee1aaa828a2f72ba5e6d00ae1343b80` 及原更新目录保留为历史证据。

r2修复打包依赖闭包、父目录创建、安装前后加载验证和失败提示；源码入口 `F1/scripts/68_package_windows_input_pause.py`，安装模板 `F1/windows_client/update_input_pause.ps1`。在全新临时目录中模拟旧g2teleop客户端且无dagger_core，解包后9文件SHA和sender导入通过，原CMD保留；删除mapper后加载探针确实失败。相关回归53 passed（0.84秒）；首次调用误用了不存在的测试路径，随后一项loopback被sandbox禁止socket，申请仅本地测试权限后全套通过，无保护断言放宽。Linux无PowerShell，以上不等于Windows安装器实际执行或现场控制验收。

F1修订目录 `/home/user/Bruno/DAgger/updates/20261015-input-pause-v31-r2/` 已上传并读回校验，脚本SHA256 `4eab9c041a7c9d90b64a3205594ca325e1ab4f99cb5b0d381309d7d6b4c9a93d`；9个payload文件与现役v31的windows_client逐一一致。机器人现役、数据和服务均未改变，无远程动作。构建清单在 `artifacts/results/20261015-input-pause-v31-r2/package.json`。
