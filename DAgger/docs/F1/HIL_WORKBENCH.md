# F1 HIL 工作台与三端操作

当前部署事实只在 [STATUS](STATUS.md) 维护；本页说明运行合同和操作方式。用于 F1-09，采集和默认导出仍为 30Hz。三图预览目标每路 10fps，其他页面约 1.33fps。它们读取已有相机压缩缓存；隐藏、切页、收起或滚出视口会取消预览请求，相机和原始采集继续。多个同时可见窗口的网络流量会相加。

4242三路线包装器与绑定的运行端配套；旧v15启动入口保留。每个新版本仍需分开完成软件、shadow和现场动作验收。`policy-shadow` 使用真实观测和模型但没有动作出口，`shadow` 保留人工演练。`teleop` / `run` 必须通过 release 接受配置、`--execute`、现场 Enter、手柄 START 和原运行合同；网页不提供运动按钮。

## 三端启动

新版本上线前完成 ARM 数值验证、只读 shadow及现场计划中的预览对照。运行配置必须绑定本包 `F1_RELEASE_MANIFEST.json` SHA，`F1_INFRA_ROOT` 必须指向依赖清单匹配的工作台目录；不能仅改 SHA 作为动作验收。

F1 现场先完成基础启动，再在4242的HIL页选择“自由接入 / 人工示教 / 策略 HIL”，复制本机三个终端命令。页面只呈现指引和状态；用途、模型、任务、视频设置均由后端JSON维护。

**终端 1 · F1 本机现场终端，任意工作目录：** 以人工示教为例，确认页面给出的本机入口与此一致后运行：

```bash
bash /home/user/Bruno/DAgger/start_f1_hil_route.sh demonstration
```

自由接入把参数换为 `play`；模型已配置并验收后才使用 `policy`。部署侧的 `F1_HIL_ROUTE_DIR` 指向 play.json、demonstration.json、policy.json，分别绑定 teleop_play、demonstration、policy_hil；包装器加载本机ROS、验收配置及显式 `F1_HIL_ROUTE_EXECUTION`。日志在本机 `F1_DAGGER_RUNTIME/onsite-powered-console.log`，本次已记录站点为 `/home/user/Bruno/DAgger-runtime/f1-09/`。

底层 `67_f1_route.sh` 默认shadow；已安装现场包装器按配置选择执行模式，实机仍须Enter与START。试玩保留诊断、不写训练图片、不入训练导出或视频队列；示教正常30Hz采集；策略路线在模型未绑定时不可启动。旧 input_check 仅作为开发者零输出检查保留，不是第四个前端入口。切换网页不改变当前会话。

三个流程均提供F710可点击图和真实F1模型的离线右臂动画。点击教学仅发生在浏览器，不能启动机器人；实际手柄操作与教学动画分开。

**终端 2 · Windows PowerShell，任意工作目录：** 隧道直连本机 F1-09：

```powershell
ssh -F NUL -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -L 127.0.0.1:7907:127.0.0.1:7906 user@192.168.31.58
```

**终端 3 · Windows PowerShell：** 在 Windows 客户端目录运行 sender：

```powershell
cd "$HOME/DAgger-F1"
.\scripts\windows_f1_sender.cmd
```

等待实际接收端回执。F710 使用 X 模式，MODE 应使左圆摇杆移动 joy.cpl 左侧 X/Y。START 授权/接管，十字键选侧后释放，RB 控臂，RT 切所选夹爪，A 切速度，Y 右臂上抬6cm、向右10cm，再回「积木demo-V0」；左臂、腰、头和夹爪保持现状。软限位只限制继续向外，向内可直接恢复；松 RB 保持人工模式。断连保护仍需释放控制后 START 恢复。

## 结束与数据目录

F1-09 的 Y 使用运行配置 `f710_home` 中「积木demo-V0」的右臂目标，来源预设 ID 为 `81bda04dd141438e8a1ae2a6df2d3fdb`。先松RB，再按Y：右臂上抬6cm → 向机器人右侧移10cm → 右臂回预设；不增加独立转腕动作；前两段允许少量姿态变化（相对起始朝向最多10度），J6不比起始命令更向外弯。前两段以base_link为参考（+Z向上、-Y向右），每段等新鲜实测到位后进入下一段；小幅稳定跟踪偏差在停稳后受限修正，大误差会停止本次Home并保持录制。Y会清空选臂：松Y/RB，十字键选右臂后释放，再按RB接管；有效RB接管也可取消进行中的Home。松RB后START可取消Home并结束示教当前条。左臂、腰、头、夹爪保持当前状态。原关节/腕部限位、输出平滑、反馈/自碰撞及停止保护保留；桶未纳入碰撞模型，现场净空需要操作者确认。配置仍保留完整来源快照SHA，但只执行右臂7轴；网页修改预设不会自动改变绑定的手柄目标。所有入口和验证边界见 [STATUS](STATUS.md)。

正式三路线v26：本次Home失败后，松开Y再按一次，会先平滑对齐实测起点，再继续同一次复位尚未完成的段，避免每次重试都追加上抬/右移距离。持续误差仍会停止，不自动反复重试。有效RB接管、START或断连会清除该路径检查点，之后Y按新的起点重新规划。避让仍为4cm/s，暂不提速；当前软件与现场验收边界见[STATUS](STATUS.md)。

人工示教用松RB后START结束当前条并保存，等待未录制后再次START开启下一条；具体见下方连续采集步骤。策略HIL的START用于接管/交还。需要结束整个运行端时，先释放手柄控制，再在4242选择“结束并保存”，或在F1原运行终端Ctrl+C；等待journal/payload正常提交后，再结束Windows sender和隧道。网页请求校验run_id/PID/start_ticks，只作用于匹配会话；失败或 `.partial` 不标为saved。

`F1_DAGGER_DATA_ROOT`（或 `--data-root`）配置独立存储根。当前站点为 `/home/user/Bruno/data/hil_data`（旧 `/home/user/lxl/dagger_data` 是历史路径），人工示教与策略分别落入 `demonstration/`、`policy_hil/`。自由接入诊断、状态、完成通知和视频队列仍在runtime；旧runtime/raw只读兼容，不需要搬动原件。数据保存及视频审核不自动授予训练就绪。

## 模型服务

F1独立包解压后从 `DAgger/` 运行下列命令。开发树则先 `cd F1`，并把下列 `$PWD/src` 改为 `$PWD/src:$PWD/../shared/src`：

```bash
export PYTHONPATH="$PWD/src"
python services/policy_server.py --mock --host 127.0.0.1 --port 8091
```

假模型重复当前 state16，仅用于协议检查，不能作为动作语义或策略能力验收。实际模型提供 `module:function` 接口 `predict(images, metadata) -> list[H][16]`，在服务器适配器内进行解码、裁剪、归一化、权重加载与反归一化：

```bash
python services/policy_server.py --adapter my_model:predict \
  --model-id pick-v1 --model-version weights-sha256 --host 0.0.0.0 --port 8091
```

只对受控机器人网络开放服务。`GET /health` 返回 model_id、不可变 model_version、合同和能力；`POST /process_frame` multipart 字段为 metadata、head、left_wrist、right_wrist。metadata 包括 state16、task、request_id、observation_id、policy_epoch、observation_monotonic_ns、chunk_steps、frequency_hz。响应回显身份和时序，另含 model_id/model_version/actions。

顺序：左臂7、右臂7、左夹爪、右夹爪。臂为硬件绝对度，夹爪0开100闭；固定30Hz，默认 H=50。模型更换、错误单位/顺序/维度、NaN、越界、旧epoch或超时会被拒绝。后端探活不发送图片；显式试推理等待请求之后的新三图和state，检查响应但不发布动作。模型配置写入后端policy.json，不在前端编辑。

## 会话后自动视频

策略 HIL 默认开启，示教可选、自由接入关闭。正常结束并保存或Ctrl+C退出后先完成 payload/journal 提交，然后入队；异常 `.partial` 不伪装成完整任务。可完整提交的会话若最后处于异常状态，片段据实截断。每个介入含前5秒、人控/保持、交还等待、实际策略输出恢复后5秒；不足部分标记截断。Home保留显示标签，不作为专家动作。

服务器安装 Pillow 与 ffmpeg，并创建独立 worker 配置，参照 `configs/video_worker.example.json`：robot_url 指向该机器4242；access_token_file 为现场安全配置的工作台访问凭证文件；cache 为服务器派生缓存。不要把凭证写进源码、命令日志或模型配置。

```bash
export PYTHONPATH="$PWD/src"
python services/video_worker.py --config /etc/f1-hil-worker/f1-09.json
```

`services/f1-hil-video.service.example` 提供服务器常驻模板（按实际目录修改），不会自动安装。worker 首次认证 claim 后登记本机视频服务状态。没有 worker 配置时任务显示等待配置；服务器离线不影响原始采集和既有本地回放。

每次单任务，双向按块传输，默认上限2MiB/s，HIL或数采活动时暂停大文件传输。任务持久化于 DAgger runtime/video-jobs，回传位于 videos；编码和模型权重始终在服务器。源manifest、journal、帧索引及逐帧payload哈希需通过；回传哈希和MP4容器核验后发布。worker完成真实解码测试。任务租约120秒，20秒续约；崩溃后重领，失败任务网页重试，部分传输按offset恢复。

30fps视频是展示时间轴，重复展示旧图会标注原始 observation/frame/time，不能计为新采样。`采纳/排除`是视频审阅记录；不会自动授权训练导出。训练仍使用原 F1 export 的源审计、独立纠正审核及实际发布 action。原始数据不自动删除。

## 验证与回退

[首次发布报告](HIL_RELEASE.md)和[三路线记录](HIL_ROUTES_RELEASE.md)保存当时结果；最新已记录版本见[STATUS](STATUS.md)。新版本现场对照应覆盖预览开/关、F710实际操作与ACK分位统计，检查30Hz真实回调、偶发间隔、写盘和积压。图片HTTP耗时不是镜头到屏幕或手柄ACK。

上线工作台之前备份现役current、dev页面、配置和服务并记录SHA；仅空闲时切换。按本次候选分别验收工作台、运行端和服务器worker；使用匹配版本的部署备份回退，不按旧文档硬编码跳回v24。旧v15入口保留可用，保留新任务及raw，禁止反向改写原数据。代码回退不恢复姿态，也不自动重放命令。

本次三路线部署结果见 [交付记录](HIL_ROUTES_RELEASE.md)。

## 人工示教 START 分条（v20）

三端仍按页面命令启动，F1 现场 Enter 只确认运行端就绪。首次 START 开始一条人工示教；结束时释放 RB、等待夹爪完成，再按 START（进行中的Home会被取消），机器人保持并后台保存。显示未录制后，再按 START 开始下一条，无需重启三个终端。保存期间 START 不排队、不自动恢复。断线进入保护保持并收尾当前条，重新连接不自动开始新条。网页「结束运行端」或 Ctrl+C 保存当前条并退出进程。

未录制及保存期间不追加三图、观测和逐帧控制日志；反馈与相机接收继续服务于健康检测，不关闭相机。新原始条目仍在 `/home/user/Bruno/data/hil_data/demonstration/`，每条完整提交后由既有 DATA/转换扫描发现。进程模型和诊断单独放 runtime/sessions。录制保持30Hz；结构完整不代表训练审核通过。自由接入、策略HIL的START语义不变，示教仍要求松RB及夹爪退出条件；Home可由START取消，不再阻塞结束录制。

### 连续人工示教的操作顺序

当前人工示教入口支持同一进程连续分条，无需重复启动三个终端：

1. 在未录制状态单击并释放START，等显示正在录制；按需选臂，按RB操作。
2. 完成任务后松RB；如果需要Y复位，可按Y并等待到位；希望提前结束时，START会取消Home并保存。夹爪动作仍需完成。
3. 单击并释放START结束这一条，等待已保存/未录制。
4. 场景摆好后，再次单击并释放START开始下一条，每条有独立目录与manifest。

保存阶段不接受排队START：过早按下不会在稍后自动开录，需要回到未录制后重新按一次。不要连续双击START。Y/Home在记录中保留审计，训练筛选排除；START与录制绑定的这套语义仅适用于人工示教，策略HIL的START仍表示接管/交还。

2026-10-13再次核对已部署v20的DemonstrationRecorder与本地SHA一致，保存完成后回到idle；现有8项离线分条测试通过，含连续两条、保存阶段不自动开始、断线不自动重启、写盘失败不可假报成功。未通过SSH发动作。

### 提示音候选（等待用户选择，未接入）

独立试听地址为工作台 `/review/hil-audio-20261013/index.html`。四个候选来自Kenney的Interface Sounds和Music Jingles（CC0），0.29/0.38秒开始提示、0.54/1.00秒完成提示；原出处、许可证、哈希和音量衰减记录随试听页manifest保存。浏览器点击才播放，不替换机器人 `Music/1KHZ-0db.wav`。

未来接入的事件语义：开始音对应录制器正式进入recording，完成音对应原始数据成功提交且可以录下一条；不是START请求、不是视频转码结束、更不代表任务成功或训练审核通过。播放异步且有界，不阻塞控制/采集，不因声音播放失败撤销已保存数据。素材与实际扬声器音量待用户确认，本轮未实现自动播放。

### 已选择并接入的提示音（2026-10-13 v21）

用户选择开始A/完成B。v21在人工示教录制器进入recording时通知开始音，在正常提交、phase恢复idle时通知完成音；音频专用线程、有界队列、aplay超时，播放失败只记录音频错误，不改变控制或已保存数据。演练不启用声音，HIL策略与自由接入不套用示教分条音效。已有8项分条测试加4项音效测试本地通过，ARM连同既有依赖共42项通过；运动控制模块保持v20逐字节一致。

桌面版已被下述浏览器版替代。在 **SSH 连接 F1 的终端** 运行：

```bash
bash /home/user/Bruno/DAgger/tune_recording_audio.sh
```

两个旋钮/滑条分别设置开始与完成音，0为静音，100为本素材最大增益；点击播放试听，点击保存音量生效。保存路径 `/home/user/Bruno/DAgger-runtime/f1-09/recording_audio.json`，采集程序每次播放前读取，不需因调整音量重启。系统总音量不改。正式采集仍由现场运行人工示教入口，Enter与START授权保持；新版本需从现有三路线包装器启动。

素材由所选原件转为16kHz单声道PCM16、峰值约-3dBFS，实际增益独立设置且默认18%；短音前400ms/后150ms静音给音频输出启动留时间。这只是音频播放，不延后录制开始或写盘提交。原Music/1KHZ-0db.wav未覆盖。

现场限制：第一次直接播放试听WAV的aplay退出0，但用户明确没有听到声音，不能写作扬声器验收通过。音量/路由尚待用户在调音窗口确认；SSH缺桌面授权导致隐藏Tk检查未完成，F1已确认有Tkinter。不得据成功退出声称可闻。

### SSH 浏览器调音修复（2026-10-13）

用户实际使用SSH而非NoMachine，Tk版因无DISPLAY无法运行，原桌面前提不适用。稳定命令 `bash /home/user/Bruno/DAgger/tune_recording_audio.sh` 已改为独立HTTP调音工具，默认4243端口，不依赖Tk或图形桌面；保持终端运行，在操作者电脑浏览器打开终端打印的**完整带片段访问码的链接**。旋钮调音量，播放从F1输出；保存后沿用已有recording_audio.json，工具退出不影响保存值或采集程序。调好可Ctrl+C退出。端口占用可加 `--port 4244`。

工具独立位于 `DAgger/tools/recording-audio-web-20261013`，只复用已发布v21的音频模块；不改不可变release、现役控制入口或机器人动作。GET不发声，播放/保存要求随机访问码，POST检查同源、音量范围与单次在途播放；不暴露任意命令/文件接口。

本地接口和音频5项测试、ARM接口测试通过；桌面浏览器实际验证两旋钮、播放测试桩和配置保存，无JS错误或溢出。F1实际无DISPLAY启动、网页和认证状态读取成功后退出，未试播或修改用户音量。本次解决“工具打不开”，未据此宣称此前扬声器无声已解决。

### 调音完成后连续采集（2026-10-13 补证）

用户随后确认已听到并保存，现役人工示教已经读取同一音量配置。调音窗口可Ctrl+C退出，不需要持续运行；正式采集时停止手动试听，避免声音重叠。按上述三端人工示教流程启动，START开始后响A；松RB、等待Home/夹爪完成，再按START结束，原始数据提交成功后响B，等已保存/未录制即可再按START。无需每条重启进程；保存中不要双击START。

声音不是录制开关，静音或播放失败不阻止保存；以页面录制状态为准。完成音表示本条原始数据已保存，不代表任务成功、视频已生成或训练审核通过。每条仍独立落盘在 `/home/user/Bruno/data/hil_data/demonstration/`。本轮已通过44项ARM回归和真实观测三条shadow连续保存；具体结果及相机不连续记录见 [当前状态](STATUS.md)，未据此代替现场运动验收。

## F1 本机独立右臂复位诊断

不需要 Windows 隧道、sender 或 DAgger 采集进程；关闭已有操控/采集会话后，在 **F1 本机现场终端**运行，工作目录任意：

```bash
bash /home/user/Bruno/reset_right_demo.sh
```

脚本先读取实测姿态、验证已绑定驱动/模型/配置，离线预演完整三段路径（已在Home的原1度到位范围内时只做短回位收敛，不重新绕行）；通过后等待现场Enter才创建动作发布端。只发送右臂7轴，腰、头、左臂、夹爪不发指令；Ctrl+C停止本次脚本。脚本复用已验证控制库，不启动HIL、手柄服务或数据采集。桶不在碰撞模型中，预演不等于实物净空验收。任一预检/反馈/输出故障均退出，不跳过避桶直返。

若提示“轨迹预演未通过，尚未发送机器人动作”，失败发生在Enter前，不是实物电机跟随失败；保留对应events.jsonl。预演通过也不等于实机到位。

只看当前反馈和数值预演、不创建动作发布端：

```bash
bash /home/user/Bruno/reset_right_demo.sh --check
```

终端日志：`/home/user/Bruno/DAgger-runtime/right-reset/console.log`；每次结构日志和模型诊断：该目录下UTC时间子目录的`events.jsonl`。不写训练数据。
