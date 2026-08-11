# G2 Robot现场代码结构

最近整理：2026-08-04<br>
G2目录：`/home/agi/DM0.5`<br>
6000D开发副本：`/home/xza_100850113/projects/DM0.5/g2_robot`

```text
g2_robot/
├── AGENTS.md       # 协作、安全和GDK查证规则
├── README.md       # 项目入口
├── g2_sort/
│   ├── config/     # DM0.5合同、旧复位锁和批准的双手Home
│   ├── deployment/ # 正式VLA客户端
│   └── operations/ # 只读诊断、锁定复位和硬件实验
├── scripts/        # 现场人员唯一推荐命令入口
├── tests/          # 无GDK纯逻辑测试
├── docs/           # 操作、运行时、状态和踩坑文档
└── logs/           # G2现场生成；同步代码时不从6000D覆盖
```

正式客户端入口：

```text
g2_sort/deployment/g2_sdk_client.py
g2_sort/deployment/g2_client/
```

正式数据流：

```text
三路RGB + 34D measured state
→ DM0.5 HTTP/75K/7892
→ 50×34 absolute action
→ 双臂14D/100Hz joint_servo_control
  + 双手20D/5Hz move_ee_pos
```

`g2_client/`职责：

- `app.py`、`cli.py`、`cleanup.py`：装配、参数和生命周期；
- `cameras.py`、`robot_state.py`、`observation.py`：严格观测；
- `dm05_backend.py`、`contracts.py`、`policy.py`：HTTP及模型合同；
- `interpolation.py`、`servo.py`：30→100Hz播放和hold；
- `trajectory_smoothing.py`：默认关闭的双臂固定端点chunk内部jerk正则化；
- `robot_control.py`：正式Split硬件分发及显式诊断模式；
- `hand_command_store.py`：原子保存最后成功O10命令空间20D；
- `control_loop.py`、`prefetch.py`：sync/async调度；
- `keyboard.py`、`telemetry.py`：软件控制和结构化日志。

当前正式脚本是`05_powered_sync.sh`；`06_powered_async_experimental.sh`只用于实验。
`operations`中的hold、旧H5复位、O10单次和拇指微动execute保持锁定；唯一获准的
恢复动作是`scripts/03_reset_home.sh --execute`。它短时复用正式Split，双臂保持
实测位置让双手回Home，再将双臂恢复到H5第0帧14D中位数；正式Powered仍只通过
`scripts/04～06`进入。

批准Home位于`g2_sort/config/hand_home_approved.json`；G2运行时只在
`logs/runtime/last_hand_command.json`更新最后成功命令，供VLA退出后的短时复位。

6000D修改不会自动出现在G2。同步时只覆盖`g2_sort/`、`scripts/`、`docs/`、
`tests/`和根README；不复制历史`logs/`、缓存、旧压缩包或SHA文件。所有真机动作仍
由Bruno在G2终端手动运行。
