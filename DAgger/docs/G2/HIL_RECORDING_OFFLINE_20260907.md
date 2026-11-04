# 2026-09-07 HIL 因果审计、共享观测与录制旁路

> 历史/专项证据：本文结论对应文内日期与轮次。当前实现及下一步统一见 [当前进度](RECORDING_AND_PROGRESS.md)，按序查阅 [文档索引](00_INDEX.md)。

本轮是6000D离线实现与验证，不是新真机验收。冻结F710执行入口、控制参数、Home、rolling
envelope和归档未修改。START/button7原始按钮只读验证随后已通过（见末节），真实Powered HIL仍未接入。

## 核对结果与证据边界

- 已完整阅读交接、F710架构、DAgger全流程、Windows入口及SUPER_IMPORTANT根因文档。
  架构早期OTG/91秒replay、Windows旧RB接管设计均属于历史，不替代冻结方案。
- 核对`g2_robot/scripts/19_powered_sync_dual_side_17d.sh`及`robot_control.py`：当前连续手路径确为
  动态名称左右各17D，normal Servo；并未迁入冻结F710。
- 重新解析`g2_robot/logs/telemetry/powered/*dual_side_17d_servo100_20260904_144901.jsonl`：
  379条JSONL，包括15条action_chunk、15条dispatch_summary、327条hand_wait_command和run_summary。
  这确认已有真机日志，不代表本轮重新跑过13.36秒实机，更不是300秒长稳验收。
- 冻结manifest与工作区有5个既存差异：`03_windows_shadow_sender.py`、`12_g2_powered_f710.py`、
  `gamepad_windows.py`、`f710_mapping.py`、`protocol.py`；本轮未改这些文件，不能宣称工作区逐文件等于归档。
- 新版g2_robot camera有原生JPEG/PNG passthrough；DM0.5旧版主要返回解码RGB。两者都未把每路时间
  信息完整传到录制接口。本轮新增的是消费**已取得消息**的adapter，不建立第二个Camera/Robot owner。
- 已在线核对[GDK 2.6.3 Camera原文](https://genie-data-support-1347244451.oss-cn-shanghai.aliyuncs.com/gdk_api_docs/2.6.3/docs/contents/reference/Python/camera.md)：
  `Image.timestamp_ns`单位为ns。文档没有确定本机设备时钟域与host monotonic的对应关系，默认写
  `gdk_device_unverified`，不根据数值大小推测PTP/Unix时间。编码枚举仍由已核实的调用方映射。

## 本轮实现

`hil_supervisor.py`新增单个pending fresh request绑定：必须在HANDOFF_HOLD/current epoch，且
`handoff < observation capture start <= request <= response`。只修改epoch或只给request ID不能恢复；
回到HIL、旧epoch、未绑定request、提前response均拒绝。调用方仍必须交付真实采集时间，不能把缓存
图像的到达时间冒充新曝光；是否重复图像还需payload审计和真实时钟验证。

`hil_episode.py`升级schema 2，`hil_causality.py`核对跨流observation ID、request/epoch、观测时间、
可用时间、response、控制序号、waypoint索引/值、ownership事件及mask。保留旧字段`request_id`作为
观测ID别名，新增明确的`observation_id`；policy request ID独立编号。原始50×34 chunk没有改变。
旧schema缺少request时间时不会静默获得新审计认证。

selected表示仲裁结果；submitted表示实际提交目标；每次SDK调用分别记录interface、joint_indices、
submitted_positions、开始/结束host时间及原样sdk_return。executed与measured允许缺失，不再要求
selected==executed，也不从成功返回推断运动完成。Mock的executed为null，反馈标记synthetic/invalid。
此轮尚未接真实SDK调用证据；结构审计通过不证明机器人执行。

`33_hil_mock_episode.py`修复首个response晚于首个控制、tick38引用未来观测等问题；独立观测时钟使用
整数有理数30Hz，而不是每3个10ms tick采一次。action index按30Hz waypoint时间推进，推理只发2个
合成chunk。18条观测在HIL和HOLD期间继续产生，60个控制tick保持原START/RB所有权语义。

`recording_observation.py`提供不可变`TimestampedObservation`、三路原始压缩frame、原始时间戳
value/unit/clock_domain、逐路host接收时间、采集开始/完成时间、state采样时间、34D有效位及来源。
`SharedObservationStream`给policy和sink同一对象；采集由外部采集worker持续驱动，不能绑到推理请求。
生产camera/state采集线程及同owner handoff仍是下一接入步骤，当前不是已部署的推理runtime。

`recording_sinks.py`提供同一旁路协议和factory：

| 模式 | 行为 |
|---|---|
| inference-only | NullSink，不保存图像；保留调用方需要的观测及轻量journal |
| hil-record | EpisodeVideoSink，后台逐帧保存原生JPEG/PNG及精确索引 |
| prebuffer-record | RingBufferSink，默认4秒，START介入时一次提交预录，持续录至关闭 |

Ring默认128MiB预算（payload字节+每观测4096字节metadata估算）、180条上限，并按时间淘汰；writer
默认192MiB/256条，**在途写入也计入容量**。两者均有独立条数上限，不把估算预算称为整个Python进程
RSS硬上限。预录边界包含两端，例如30Hz的4秒窗口可保留121条。容量淘汰与正常时间淘汰分开计数。
trigger/submit应串行在采集/event worker调用，不能把预录批量处理塞进100Hz控制owner。

writer执行hash、JSON、磁盘写入和fsync。溢出整批拒绝，记录drop count及首末ID；磁盘失败、abort
保留.partial。close超时不在活跃writer下改名；重复close不能将abort升级为completed。正常封口仅表示
写入完整，随后仍需内容审计。强杀可能只剩.partial且没有manifest，审计会拒绝。

`recording_audit.py`检查payload字节数/hash、逐帧索引、三路完整性、重复/回退时间戳、ID缺口、时钟域
改变、无效反馈；只报告host接收偏斜，未知时钟域不计算三路曝光偏斜。`34 --payload-dir`进一步核对
journal与payload的observation ID和完整采集metadata。超过预录窗口的journal不能直接当成同等长度的
payload episode；正式转换必须选择录制覆盖段并保留其policy因果依赖，当前不会自动补造丢失帧。

`36_encode_recording_review.py`在后台录制完成后，离线生成三路lossless RGB MP4及逐帧timestamp
JSONL，使用ffprobe核对帧数并完整解码。原始图像与timestamp sidecar保留。MP4的CFR PTS仅供审阅，
不替代真实曝光时间，也不是DM0.5训练视频认证；不使用100Hz控制tick推算视频帧号。

## 本轮新验证

- 6000D最终完整回归：**134 passed / 6 skipped**；skip来自缺少Ruckig/Pinocchio。
  首次沙箱运行的loopback权限失败已通过允许本机socket的完整回归补测，不修改transport代码。
- 新增故障覆盖：未来/丢失observation引用、时间错配、response晚于选择、control序号/owner异常、
  stale request与迟到chunk、缺失反馈、合法selected/submitted差异、慢盘、在途容量、溢出、磁盘异常、
  abort/重复close、close超时、重复设备时间戳、payload损坏及journal/payload字段错配。
- 同一33主链在三种模式得到完全相同的60条selected/control_source序列；record模式均通过payload
  关联审计。Mock仍为无硬件合成序列，不称真实执行闭环。
- 35的6秒合成采集：180组三路帧、HIL期间推理暂停但采集继续；预录121组，最终180组、零drop。
  此轮2×2 PNG样本的采集+submit P95/max约11.77/50.50µs，峰值预算777240字节；仅是小载荷开发机
  smoke，不能推出真实分辨率、真实磁盘或G2 100Hz控制抖动已验收。
- 三路审阅视频每路180帧、完整解码通过。所有journal/payload/video manifest均保持training_ready=false。

证据目录：`logs/offline/20260907_hil_recording/`，包括`pytest.txt`、`compile.txt`、
`hil_final_*.txt`、`linked_audit.json`、`prebuffer-record.txt`和`review_video.txt`。

## 离线复现与后续门

以下命令不导入GDK；每次选择新的输出目录，已有文件拒绝覆盖：

```bash
cd /home/user/xza/G2Teleop
PYTHONPATH=src python3 scripts/33_hil_mock_episode.py /tmp/hil_new --recording-mode prebuffer-record
PYTHONPATH=src python3 scripts/34_audit_hil_episode.py /tmp/hil_new --payload-dir /tmp/hil_new_payload
PYTHONPATH=src python3 scripts/35_recording_sink_mock.py /tmp/prebuffer_new
# 仅在已有ffmpeg/ffprobe的离线环境执行，不在G2安装系统包：
PYTHONPATH=src python3 scripts/36_encode_recording_review.py /tmp/prebuffer_new /tmp/review_new
```

剩余：Windows sender到G2无输出HIL全链验证；真实三路消息/时间域/重复帧与state年龄采样；不同真实分辨率、
持续录制、慢盘时的CPU/RSS/I/O及控制周期配对；同一owner从policy双17D连续Servo到F710
changed-only手意图的执行语义交接；视频、三路时间戳、34D反馈与action label严格训练审计。
本轮不提供Powered启动命令，也未启动Robot、相机、Servo、Home或推理服务。

## G2同步与纯逻辑复核

本轮20个明确文件已直接scp至`agi@192.0.2.8:/home/user/xza/G2Teleop`（随后两个时间顺序文件
同步最终修正版）；未同步任何Powered控制入口。G2 Python 3.12.3纯逻辑回归**31 passed**，
33预录Mock和34 journal/payload关联审计均通过。远端输出在`/tmp/hil_recording_20260907_final`
与`/tmp/hil_recording_20260907_final_payload`，本地完整终端证据为
`logs/offline/20260907_hil_recording/g2_pure_validation.txt`。
远端未执行视频编码、未装系统包、未初始化GDK。审阅视频编码证据来自6000D。

最终补充审计覆盖采集时刻的ownership事件历史：先发生的观测保留先前POLICY/HIL标签，
不能因处理它时已发生START就追溯改写采集时状态。最终完整回归仍为134 passed / 6 skipped。
早期`hil_v2_*`目录仅保留本轮中间产物，复现最终结果请使用`hil_final_*`与linked_audit.json。

## Windows START现场只读通过（同日用户提供输出）

设备`Controller (Wireless Gamepad F710)`，11个按钮。独立原始按钮probe记录六对button=7
DOWN/UP，pressed_buttons在[]与[7]之间切换，持续时间分别172、140、1422、156、125、125ms。
因此物理START映射到pygame button7已现场只读确认。之前“按START无反应”来自旧只读脚本未显示
START字段，不能据此判定按钮失效。结构化原始时间证据保存为
`logs/offline/20260907_hil_recording/windows_start_readonly.json`，来源是用户粘贴输出，非自动采集。
这些时间属于Windows host monotonic，不与G2控制时钟直接相减。

未覆盖：Windows reader/mapper的hil_toggle_id、网络协议、G2 supervisor实际状态切换和真实policy
恢复；不得把本项扩大为Powered HIL验收。本次只记录证据，无新增离线测试或机器人操作。
下一现场门是sender→G2零输出HIL验证；具体入口准备后再由用户启动。
