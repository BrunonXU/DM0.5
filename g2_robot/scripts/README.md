# G2现场脚本

所有脚本自动加载`/home/agi/app/env.sh`，默认连接75K服务
`http://10.130.153.114:7892/process_frame`，新产物只写入项目`logs/`分类目录。
可通过环境变量`POLICY_ENDPOINT`显式覆盖服务地址。

## 日常顺序

```text
00_preflight → check_hand_state → 01_shadow → 05_powered_sync
```

| 脚本 | 作用 | 是否发送动作 |
|---|---|---|
| `00_preflight.sh` | 端口、GDK、34D和三路相机 | 否 |
| `check_hand_state.sh` | O10双手原始状态 | 否 |
| `01_shadow.sh` | 真实观测与75K推理 | 否 |
| `03_reset_home.sh` | 唯一正式Home：双手后双臂 | 预览否；execute是 |
| `04_powered_guarded_smoke.sh` | 1/3/6个30Hz区间的严格安全门诊断 | 是 |
| `05_powered_sync.sh` | 当前正式75K Split同步推理 | 是 |
| `06_powered_async_experimental.sh` | Split异步实验 | 是 |
| `stop_runtime.sh` | 向正式客户端发送SIGINT | 不新增动作 |

当前正式硬件下发固定为Split：模型仍输入/输出34D，但双臂前14D以100Hz发送给
`joint_servo_control`，双手后20D以5Hz发送给
`move_ee_pos(group=dual_tool,target_type=o10_t2)`。两部分不会重新合成一个34D
Servo请求。

同步完整任务示例：

```text
./scripts/05_powered_sync.sh --duration-seconds 45
```

正式Sync入口默认固定使用120ms boundary blend和双臂chunk内部jerk权重10；无需
重复传入这两个参数。Async实验入口不继承该配置。

持续运行直到按`q`/`Ctrl+C`或发生控制故障：

```text
./scripts/05_powered_sync.sh --continuous
```

连续模式没有任务完成检测，不会因为抓取/放置已经完成而自动退出；不得无人值守。

省略`--duration-seconds`时默认10秒模型动作。异步只通过明确标为experimental的
入口运行：

```text
./scripts/06_powered_async_experimental.sh --duration-seconds 10
```

同步是当前基线。异步能隐藏多数约0.47～0.52秒HTTP等待，但真机任务效果没有优于
同步，跨chunk切换反而更大。

`02_servo_hold.sh --execute`、旧H5复位和拇指微动execute仍有软件锁。O10全零在
GDK 2.6.3命令语义中是开手姿态，不能把重启后的零反馈直接当作安全hold目标发送。

当前批准Home来自人工确认的第9条成功命令，与H5首帧最大仅差0.007194rad。Split
Powered每次成功命令更新`logs/runtime/last_hand_command.json`。只读查看Home：

```text
./scripts/03_reset_home.sh
```

VLA按`q`完全退出后，执行Split双手+双臂复位：

```text
./scripts/03_reset_home.sh --execute
```

它要求最后命令在30分钟内。先初始化GDK并显示双臂行程，再输入`ok`倒数5秒；双手
以5Hz最多8步回Home，随后双臂以100Hz在默认5秒内回H5第0帧14D中位数。若曾重启、
人工掰动或物理急停，不得使用旧锚点复位。

所有真实Powered入口只接受`ok/OK`确认，并倒数5秒。当前Split配置的双臂使用
low-latency，GDK不提供碰撞保护，必须由专人全程握住物理急停。
