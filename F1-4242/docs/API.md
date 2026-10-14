---
updated: 2026-09-24
status: implemented
scope: F1-4242 HTTP API
---

# F1-4242 接口合同

数据驱动实机回放：GET `/api/replay/review`、POST `/api/replay/start`、POST `/api/replay/align`、GET `/api/replay/reports/<id>` 及一次性检查票据合同见 [REPLAY](REPLAY.md)。普通 `/api/actions` 不接受客户端 `kind:replay`；进度、停止和控制租约沿用本文接口。

本地 HTTP：`http://192.168.31.58:4242`，或经过既有 SSH 隧道的 `http://localhost:4242`。
浏览器 → 本机后端 → ROS 适配器；浏览器不直接执行 shell、UDP 或 ROS 命令。
机器能力和验证状态以 `/api/state`、`/api/robot` 为准，路由存在不表示实机动作已启用。

## 请求、会话与错误

- 所有 JSON 响应包含 `ok`；失败包含 `error`。请求/响应禁止 NaN/Infinity。
- GET 可匿名查看状态、相机、预设、数据清单。数据文件本体不提供任意路径下载接口。
- POST 必须同源，带 `Origin`、`X-F1-Request: 1`，JSON 对象不得超过 256,000 字节。
- 会话 Cookie：`f1_console_session`，HttpOnly、SameSite=Strict。控制码、令牌和 Cookie 均不应放 URL。
- 400：请求格式错误；401：未认证/码错误；403：Host 或同源检查失败；404：接口/文件不存在；
  409：输入、控制权或动作状态冲突；429：控制码限流，含 `retry_after_s`；503：相机无新鲜图像。
- 控制码按连接来源 IP 限制连续失败：5 次/60 秒；全局 30 次/60 秒。忽略 `X-Forwarded-For`。
  经同一 SSH 隧道访问的浏览器可能共享来源 IP。限流在服务进程中维护，重启后重置。

## 身份与控制权

| 方法/路由 | 请求 | 响应与行为 |
|---|---|---|
| GET `/api/session` | — | `authenticated`、`control_code_configured` |
| POST `/api/login` | `{control_code}` | 验证控制码，只认证会话；不占控制权，不启动动作。旧 `{token}` 访问码仍兼容 |
| POST `/api/logout` | `{}` | 退出认证；如果自己占用控制权，请求停止，停止完成后才释放锁 |
| POST `/api/control/acquire` | `{control_code, owner?}` | 每次验证控制码后获取共享租约，返回 `authenticated:true, lease_id, expires_in_s`；owner 为 1–80 字符，默认现场操作者 |
| POST `/api/control/heartbeat` | `{lease_id}` | 持有者续租，返回 `expires_in_s` |
| POST `/api/control/release` | `{lease_id}` | 请求停止并释放；返回 `stopped, action`。执行器未停止时保持锁 |
| POST `/api/stop` | `{lease_id}` | 请求停止本工作台动作；不自动释放控制权，不是硬件急停 |

默认租约 3 秒。心跳应每秒发送；关闭网页或断网后由后端停止并释放。
写接口需已授权会话；动作遵循 acquire → plan（关节）→ actions，网页首次输入控制码启用操控后按会话复用授权。当前交互见 [USER_GUIDE](USER_GUIDE.md)。控制码不持久化到网页存储；底盘取得控制权后，仍要明确按住方向键才发 base_jog。
获取控制权不会发动作；旧访问码认证也不能绕过 `/api/control/acquire` 的控制码验证。
Pico 与网页沿用同一个跨进程 `flock`，另有 ROS 发布者、反馈新鲜度、配置合同检查。

控制码只以随机盐和 PBKDF2-SHA256（600,000 次）的摘要保存为 `runtime/control_code.json`，文件权限 0600。
部署时在 F1 本机通过标准输入设置，不放源码、网页、URL 或发布清单：

```bash
cd /home/user/Bruno/F1-infra/current
read -r -s -p '新控制码: ' f1_control_code
printf '\n'
printf '%s\n' "$f1_control_code" | python3 control_auth.py --runtime /home/user/Bruno/F1-infra/runtime
unset f1_control_code
```

## 状态与相机

| GET 路由 | 返回 |
|---|---|
| `/api/robot` | `robot`：机器 ID/IP、具名关节分组、角度限位、配置版本和运行能力 |
| `/api/state` | 机器人反馈快照、`authenticated`、`capabilities`、`control`、`action`、`system_health`、`services`、`pico`、`data` |
| `/api/cameras/head.jpg` | 头部最新 JPEG；图像接收年龄 >2 秒返回 503 |
| `/api/cameras/left.jpg` | 左腕最新 JPEG，单独判断新鲜度 |
| `/api/cameras/right.jpg` | 右腕最新 JPEG，单独判断新鲜度 |
| `/api/storage` | `health`：磁盘、RAM、选定进程快照；容量估计不是录制保证 |
| `/api/system` | 运行目录、版本、配置验证合同、ROS 发行版、数据模式和能力 |
| `/api/events` | 最近最多 300 条本进程事件，倒序；记录不包含控制码 |
| `/api/dev/revision` | `revision`：远端使用同步完成后的 sync.json 签名；本地回退为文件路径/大小/纳秒级时间戳签名；不读取模型大文件正文 |

`joints[name].position` 为 **度**，`age_s` 为本机接收年龄；未知/失联不是零位。
夹爪 `position` 为当前适配器定义的 0–100；不是毫米。
`control` 含 `owner, owner_label, lease_id, expires_in_s, available, stopping, reason, code_configured`。
只有当前控制浏览器会收到自己的 `lease_id`。

## 显式动作

POST `/api/actions` 必须先认证，并持有有效 `lease_id`。
响应为 `{ok:true, action:{id,status,...}}`，代表请求进入生命周期，不代表实物到位。
随后 GET `/api/actions/<id>` 或 `/api/state` 跟踪完成、拒绝、取消、停止和反馈验证结果。

| kind | 除 `lease_id` 外的字段 | 语义 |
|---|---|---|
| `joints` | `targets:{arm_l_j1:角度,...}` | 具名关节目标，单位度；未显式给出的同组轴从新鲜反馈补齐，不隐式回零 |
| `lift` | `level:1..4` | 腰部三轴升降档位，身体另外两轴按实测保持；不是厘米 |
| `gripper` | `side:'left'或'right', position:0..100` | 单侧夹爪位置目标 |
| `base` | `vx,vy,wz,ttl_s` | 线速度 m/s、角速度 rad/s；已开放现场点动；持续续期仍最多 6 秒 |
| `base_jog` | `targets:{vx,vy,wz},ttl_s` | 兼容前端写法，转换为 `base` |

`POST /api/actions/plan` 同样需要认证和 `lease_id`，接收 `kind:joints/lift` 及上述字段，返回 `{ok:true, plan:{origin,targets,motion_s,max_action_s,fixed_speed}}`。它只计算预览，不创建命令发布者、不占动作 ID。正式执行会重新读取反馈并规划，预览不是之后必定可执行的承诺。

F1-09 使用固定速度与加速度配置，旧 `duration_s` 在本机固定速度模式下忽略。取消 5° 单次步幅，保留本机关节范围；规划运动最多 5.7 秒，预留连续到位检查，执行预算 6 秒。大幅目标超预算返回 409 并提示拆分。双臂、身体、头部、升降、夹爪及底盘已按本机profile开放；当前姿态入口使用下文kind:pose；软件接口验证不等于实物动作验收。身体单独使用 200Hz 五次曲线和绝对调度，身体+双臂混合请求会要求分组执行。动作结果 `command_timing` 包含 `target_hz,samples,actual_hz,p95_gap_ms,max_gap_ms,scope`（应用下发时刻，不是电机内部 servo 或 DDS 到达时刻）；调度间隔超过 25ms 会失败停发。参数与停止语义见 [POSE.md](POSE.md)。

`/api/state.command_activity` 给出 `ready/error/policy/quiet_s/active`。姿态动作按实际 RMW 命令来源检查；待命 publisher 不再直接拦截，执行中外部命令会锁存冲突并中止自身输出。该监听不是硬件命令仲裁，不能阻止其他程序先把命令送入驱动。
不要将 `completed`、命令已发送和 `feedback_verified` 混为一谈。

## 姿态预设

| 方法/路由 | 请求/响应 |
|---|---|
| GET `/api/presets` | `{presets:[...]}` |
| POST `/api/presets` | `{name,joints:{关节名:度},grippers?:{left:0..100,right:0..100},robot_id?,source?}`；返回 `{preset}` |
| POST `/api/presets` | 或 `{name,capture:true,joint_names:[...]}`；逐轴读取新鲜实测后保存 |
| POST `/api/presets/import` | `{presets:[...]}`；一次 1–100 条，全部校验后原子写入 |
| POST `/api/presets/delete` | `{id}` |

写入需要认证但不需要控制租约。保存/导入/载入预设均不执行机器人动作。
格式 `f1_console_preset_v1`，记录机器人、度单位、创建时间、来源和 profile SHA。导入显式 units 必须为 degree，旧文件省略 units 时按 degree 兼容。前端逐项导出使用 `{schema:"f1-console-presets-v1",robot_id,presets:[一项]}`。profile.default_presets 按 key 仅初始化一次，默认 LXL XY 包含双臂 14 轴，用户已有项不覆盖；删除后由 presets.defaults.json 保持删除状态。

## 本地数据浏览与审核

| 方法/路由 | 请求/响应 |
|---|---|---|
| GET `/api/data` | `{root,available,episodes,summary,recording,checked_at,error}` |
| GET `/api/data/catalog` | 同 `/api/data` |
| GET `/api/data/episodes/<id>` | `{episode}`，ID 为列表返回的 24 位十六进制不透明值，不是文件路径 |
| GET `/api/data/recording` | `{recording}`，只读观察，无启动/停止副作用 |
| POST `/api/data/reviews` | `{episode_id,status:'pending'或'approved'或'rejected',notes,expected_source_signature}`；需要认证，返回 `{review}` |

Episode 字段：

- `id,name,relative_path,format,size_bytes,file_count,modified_at`；文件时间明确为修改时间。
- `started_at,duration_s,message_count,topics` 来自 rosbag `metadata.yaml`，不使用 mtime 伪装录制日期。
- `files:[{name,relative_path,size_bytes,modified_at}]`，原始文件不打开读正文。
- `topics:[{name,type,message_count}]`；`metadata_status` 为 `parsed/missing/unavailable`。
- `metadata_sha256,source_signature,review,preview_url`。`preview_url` 仅当原始路径和 metadata SHA 匹配已有抽帧证据时给出，否则为 null。

Review 字段：`status,notes,updated_at,reviewer,source_signature,metadata_sha256,stale`。
来源变化后返回 `status:'pending',stale:true,previous_review`，旧意见不自动沿用到新数据。
提交必须带打开条目时的 `expected_source_signature`；来源已变则拒绝并要求刷新重审。
审核只写独立 `runtime/data_reviews/<id>.json`；不重命名、删改 MCAP，不配置传输/转换。

`source_signature` 是路径、文件大小/mtime 和 metadata SHA 的轻量变更检测，**不是完整 MCAP 内容哈希或完整性验收**。
审核结果仅表示操作员给出的内容意见，不自动等于通过训练监督合同。

Recording 字段：`state,control_enabled:false,processes,checked_at,message,integration,reason`。
`observed_active` 只表示 `/proc` 观察到指向此数据目录的 `ros2 bag record`；
`no_process_observed` 表示未观察到，不能保证所有形式的录制均不存在；无法扫描时为 `unknown`。
这个路由仅表示既有 LXL；独立 LeRobot 录制使用下方单独接口，二者不能合并推断状态。

## 模型资源

URDF/网格为同源静态资源，由网页模型模块按明确的关节映射读取 `/api/state`。
静态模型视图不执行 IK、不发布关节目标，不作为碰撞/机械机构验收。
具体模型来源、资产入口与已知映射边界见模型模块说明。


## 独立 LeRobot 录制接口

用户已授权独立上肢数采。写操作只需认证，不需要动作租约；与 Pico 动作控制并行。
GET `/api/recording/state` 返回 `{ok,recording}`：
`mode,available,ready,ready_reasons,output_root,contract,task,fps,frames,elapsed_s,training_accepted,last_error,last_saved,review`。
mode 包括 `not_started/starting/idle/recording/finalizing/review/error/unavailable`。
当前独立合同为 `f1_upper_16d_v1`，`source_basis=verified_pico_session_and_graph`（只接受已验证的独立 Pico 会话，不是逐消息 GID），机器 profile 10fps；客户端必须读返回值，不能写死 30fps。

| POST 路由 | JSON | 语义 |
|---|---|---|
| `/api/recording/start` | `{}` | 服务不存在时先准备独立订阅进程；就绪后再次明确开始。不会启动 Pico、动作或旧 LXL |
| `/api/recording/stop` | `{}` | 停止并处理视频，进入待确认或 recovery；不会自动提交 |
| `/api/recording/save` | `{}` | 保存待确认片段；完整数据提交 LeRobot，不完整观测仅保留 recovery |
| `/api/recording/discard` | `{}` | 将待确认片段移入本任务 discarded 隔离目录；不删除，也不改旧 LXL |

写操作可能需编码器收尾/复制媒体，客户端建议 120 秒超时；同时可继续轮询 GET 状态。
断开网页请求不自动中止录制或提交事务。成功返回更新后的 `{ok,recording}`，状态冲突为 409。
`review` 包含 `frames,duration_s,training_ready,coverage_fault,videos`；`last_saved.kind` 为 `committed` 或 `recovery`。

GET `/api/recording/review/<head|left|right>.mp4` 读取当前待审核视频。
GET `/api/data/video/<episode-id>/<head|left|right>.mp4` 读取已提交/派生视频，支持 HTTP Range/206。
数据列表中的 `source:'lerobot'`、`format:'lerobot_v3_upper16'` 与旧 rosbag 区分，`videos` 给实际播放 URL。
`size_bytes` 仅训练包大小，`total_disk_bytes` 包括保留源与 alignment；新录制不会自动上传。

GET `/api/data/analytics/<episode-id>` 返回曲线：
`timestamps_s,state,action,channel_names,units,duration_s,sample_count,source_frame_count,downsampled,notes`。
LeRobot 上肢为 16 维；至多 2,000 点（含首尾），带 `action_held,action_source_age_s`。
`timestamps_s` 使用输出视频 `frame/fps` 轴，`source_timestamps_s` 保留真实相机 reference 间隔；二者不可假装同一时钟。
完整合同、DDS writer epoch、夹爪 hold 和采集条件见 [独立录制说明](RECORDING.md)。

## 网页遥操会话

`GET /api/teleop/state` 返回 `{teleop:{phase,variant,id,available,can_confirm,can_stop,legacy_config,log_tail,error}}`；GET 无启动副作用，状态每秒轮询。`nonce` 仅在当前浏览器拥有待确认会话时返回。

所有写接口沿用登录 cookie、同源 Origin 与 `X-F1-Request: 1`，不接收脚本路径或 shell 文本：

| POST | JSON | 行为 |
|---|---|---|
| `/api/teleop/prepare` | `{"variant":"upper"}` 或 `lxl` | 固定进程组启动只读准备；互斥检查后等待确认 |
| `/api/teleop/confirm` | `{"session_id":"...","nonce":"..."}` | 仅发起者、有效 nonce、无网页动作会话可确认 |
| `/api/teleop/stop` | `{"session_id":"..."}` | 取消准备或停止本网页会话，不以动作租约/录制结束为门槛 |

成功 200 返回 `{ok:true,teleop:...}`；未登录 401，同源校验失败 403，状态冲突/未知版本/离线模式 409。POST 成功只代表操作受理，以后续 phase 为准。停止接口不接管现场启动的外部 Pico。详见 [Pico 使用说明](PICO.md)。


## v12：相对升降与固定基础启动

- 网页仅首次进入操控模式调用 `/api/control/acquire`；之后按原租约 heartbeat/plan/actions，手动 release 或失效后重新输入码。acquire 接口自身仍每次验证密码；密码不持久化。
- GET `/api/waist/preview?delta_m=0.02`：读取实时状态并返回 `plan`（targets、motion_s等），不创建发布者、不取得租约、不发送动作。位移限 ±0.1m；非法/不可达返回400。模型单位不代表标定后的绝对高度。
- POST `/api/actions/plan` 和 `/api/actions` 支持 `{kind:"waist_height",delta_m:0.02,lease_id}`。目标由后端实时计算，客户端 targets 不作为升降执行依据。原认证、租约、6秒、停止合同保持。
- POST joints 可同次包含身体与头部，两组各补齐新鲜实测，200Hz同步；与双臂混合仍拒绝。新增躯干/头部模型地面检查，失败下发前拒绝。
- GET `/api/startup/state` 返回 `startup:{can_start,reason,phase,existing_pids,script,ec_script,script_verified,scope}`，只读。
- POST `/api/startup/start` 请求 `{confirm:true}`，要求同源已认证、无控制租约、无动作及 Pico；仅启动绑定哈希的固定原 start_all.sh。返回 accepted/unit/log/message；409为已有节点、脚本变化等拒绝。系统服务的 active 不等于设备反馈已就绪。


## v13：统一姿态任务（时限与进度以文末v16为准）

GET `/api/pose/preview?spec=<URL编码JSON>` 无动作、无租约；spec格式为 `{kind:"pose",targets:{arm_l_j1:10,head_j2:5},grippers:{left:30},waist_delta_m:0.02}`，各目标字段可省略但至少包含一项。只读预览返回 `plan:{targets,grippers,parts,motion_s,max_action_s,no_motion,mode:"synchronized",scope_complete:true}`。`parts`逐项为id/label/status，status为at_target或pending。不可达、未知/过期反馈、越界或超预算返回400。检查不创建命令发布端。

POST `/api/actions/plan` 和 `/api/actions` 接受同一个pose对象加lease_id；执行前重新验证整套目标。`waist_delta_m`可与body_j1/j2、手臂、头部、夹爪共同使用，但不能与body_j3/j4/j5角度同时指定。基于本机URDF的相对位移不能视为已标定绝对高度。

结果返回 `parts` 和 `no_motion`（全部已到位时true）、commands_sent、feedback_verified；执行进度也包含parts。所有参与部位到位才成功。中止/失败保留最后观察的部位状态；这不代表缺失反馈的部位已物理停稳。六秒预算作用于整个任务，不是每个部位。旧路由继续兼容。

### 0.5.1 姿态诊断与分段草稿（历史，v16已取代）

`GET /api/pose/review?spec=<JSON>`：只读，不要求控制租约。输入同 `kind:pose`。返回 `review.executable`、`issues[{part,label,code,message,...}]`、`plan`（可执行时）、`segments[{index,spec,motion_s,parts}]`（仅时长超预算且全路径检查通过时）。传输成功不等于动作可执行。`origin_out_of_range` 附 `joint/current/limits`；`duration` 附 `motion_s`。

分段不是服务端队列，也没有自动执行 API。客户端每次明确选择一段后仍用原 `/api/actions/plan` 和 `/api/actions`，携带有效租约，再做实测校验。任一非预算错误均不返回可执行的分段建议。

## v16 / 0.6.0：一次提交的完整姿态任务

`kind:pose` 保持原请求字段与认证方式。GET review/preview、POST actions/plan 和真正 execute 使用同一完整规划逻辑；合法长动作直接可执行，不再要求客户端提交中间段。review 的 `segments` 保留空数组兼容旧读者，不提供后台自动重试或绕过异常起点的接口。

plan 增加 `estimated_total_s`（运动加最少到位观察）、`checkpoint_budget_s`、`checkpoint_count`；`max_action_s` 在 pose 中表示该整项任务实际截止时间。配置 `max_pose_task_s` 为独立总上限，默认/最大60秒；原六秒用于内部监测窗口。固定速度/加速度、路径、限位、控制来源和反馈新鲜度不变；旧动作类型仍使用原预算。

一个 action ID 覆盖整项连续轨迹，progress_detail 含整项 `elapsed_s/progress/estimated_total_s/parts/feedback_arrived`，result 可含 `checkpoint_checks`。检查点是后台跟随校验，不是新的动作或用户步骤；进度为1不能替代 `status=succeeded && feedback_verified=true`。取消、租约过期或失败后无自动续跑。

v16–v19对头部J2约−27.64°的异常起点返回 `origin_out_of_range`，整套零输出。v20新增以下明确确认的合法边界回程，不放宽目标范围。


## v20 / 0.6.4：头部异常起点回程

仅当本机明确启用并绑定已审计驱动时，`kind:pose`可以处理显式包含head_j2且实测距±25°边界不超过配置幅度的目标。预设与手动目标遵守相同命令范围，不因预设身份跳过检查。

- 只读plan/review在需要时返回 `plan.head_boundary_recovery={joint,origin,edge,direction,final_target,outside_deg,limits,max_outside_deg,timeout_s,driver_sha256,speed_controlled:false,scope}`，mode为 `boundary_then_synchronized`。不会创建发布器或自动启动。
- 执行请求必须带 `head_boundary_recovery_ack:true`，严格布尔；缺少确认时零输出拒绝。网页说明首步速度与取消边界，不重复索取已持租约操作者的控制码。
- 回程阶段在 `action.progress_detail.phase` 返回 `head_boundary_observation`；只发一次合法边界目标，严格回界并观察至少150ms后，重新规划完整目标。取消、异常、再次越界不自动重试。
- `action.result.head_boundary_recovery` 保留 `status/recovered/cancelled/commands_sent/boundary_command_sent/boundary_command_attempted/delivery_uncertain/feedback_position_deg/elapsed_s/feedback_scope`。失败也保留已发送记录。即使剩余动作无需移动，整项 `commands_sent` 仍为true、`no_motion` 为false。
- 首个边界目标的 `speed_controlled:false` 不代表后续普通轨迹失去速度限制。`physical_stop_confirmed:false`明确软件取消不能撤回驱动已接收的目标。确认范围是HAL接收，不宣称PDO采样有效性或全身防碰撞。

F1-09本次头部J2回0已完成，详见 [状态](STATUS.md)；常规起点在范围内时不触发回程确认，直接沿普通完整姿态任务执行。


## v21：完整姿态生命周期

发布器准备结束后的完整重规划可更新本次预计截止时间，整项仍从原始task_start计算最多60秒；若准备已耗尽完成目标所需时间，在首个输出前拒绝，不先执行再按旧期限中断。选中但已到位的部位同样监控外部命令来源，无需为它们创建发布器。终态写入后收到stop，保留原成功/失败/取消状态，避免日志收尾阶段留下永久stopping。此项部署状态以 [STATUS](STATUS.md) 为准。


## v22 / 0.6.6：到位实测记录

成功的 `kind:pose` 在 `action.result.arrival_feedback` 中增加该次到位判定使用的实际反馈。该字段沿现有动作结果保存到 `action_result` 事件，可通过 `/api/events` 读取；旧历史不会补造字段。

- `scope: selected_targets_only`：仅包含实际被本任务到位判定覆盖的目标，含已有规划规则补齐的同组关节；完整22轴复位包含全部22轴。
- `basis: received_ros_feedback`、`freshness_scope: gateway_receipt_only`：记录ROS/HAL接收证据，不声明独立硬件PDO采样有效性或实物标定。
- `profile: {id, version}`、`observed_monotonic_s`、`minimum_confirmation_s: 0.15`：当前配置身份、该轮读取时钟及最低观察窗口。单调时钟只适合同一进程/启动内比较，事件的 `at` 为结果写入时间。
- `joints` 按HAL名称、`grippers` 按left/right列出 `target, actual, error, tolerance, age_s, receipt_seq`，存在时附 `source_stamp`。关节 `unit: deg`，夹爪 `unit: 0-100`；误差为 `actual - target`。

实测直接复制自用于成功判定的同一批反馈，不能用目标或之后的状态快照回填。静止确认 `no_motion` 同样记录；执行器返回取消或失败时不生成此成功证据。字段存在只表示执行器曾完成这轮反馈判定；调用者仍须检查外层动作 `status=succeeded` 和 `feedback_verified=true`，不能将其当作取消后的机械停止证明。只在终态生成一次，不额外采样、等待或发送命令，也不改变到位容差。部署状态见 [STATUS](STATUS.md)。

## HIL会话结束与原始数据回看

POST `/api/hil/finish`：沿用认证、同源和X-F1-Request校验；body为`{session_key:{run_id,pid,start_ticks}}`，须匹配新鲜状态及接收端finish_session_v1能力。不获取运动租约、不启动控制，响应requested仅表示请求已写入；等待HIL状态completion.phase=saved后才是提交成功。完成状态为finalizing/saved/ended/failed/interrupted，saved提供DATA episode_id。

GET `/api/data/hil/<id>/timeline`：原始观测数、实际时间跨度与频率。GET `/api/data/hil/<id>/<camera>.jpg?t=<秒>`：读取同一观测时间对应原JPEG，header包含X-F1-Observation-Id及X-F1-Recording-Time。只接受目录中的ID、固定相机名称和有限非负时间；拒绝越界、符号链接、索引/帧哈希错误。

### `POST /api/data/delete`

需已有登录和同源请求，不需要动作租约。请求：`{"confirm":true,"episodes":[{"id":"24位目录ID","source_signature":"目录返回的签名"}]}`，1–100条且不可重复。

预检失败返回409且不删除任何条目；未登录401、跨源403。通过后返回 `ok:true, complete, transaction, results`，逐条 `state` 为 `pending/staged/raw_deleted/deleted`，失败附 `error`。`complete:false` 必须提示局部清理失败并保留事务ID，不能显示全部成功。客户端超时不代表服务端未执行，应刷新目录并核对事务记录，不能自动重试。删除范围和并发约束见 DATA 文档。
