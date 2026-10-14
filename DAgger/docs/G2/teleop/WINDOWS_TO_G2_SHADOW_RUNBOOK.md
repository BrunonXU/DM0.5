# Windows F710 → G2 直连 Shadow 全流程

状态：**Windows F710映射、纯网络Shadow和相对Pink IK离线严格回放均已通过；零输出Live IK
Shadow receiver已实现，等待操作者现场验收。** 第4--6节是旧的no-GDK网络验证，第7节才读取
真实G2状态；两条路径都无法控制底盘、机械臂或灵巧手。

## 1. 固定架构

```text
F710（X模式，只插Windows笔记本）
  -> pygame/SDL2读取
  -> F710Mapper：换侧/模式/deadman/断联互锁
  -> 有界末端意图：side + mode + 3 axes + g_dot + c
  -> Windows 127.0.0.1:7904
  -> 笔记本到G2的直接SSH端口转发
  -> G2 127.0.0.1:7904 no-GDK receiver或零输出Live IK receiver
  -> ACK + JSONL日志
```

6000D服务器只负责开发和代码同步，**不在运行链路中**。F710接收器绝不再插G2。协议不存在
chassis 字段，也不存在关节位置、绝对末端位姿和 GDK 命令。

## 2. 第一次把代码拿到 Windows

若已有可访问的 Git 远端，在 PowerShell 中执行：

```powershell
git clone <G2Teleop仓库地址>
cd G2Teleop
```

若还没有单独远端，可暂时从服务器复制，把 `<SERVER>` 替换为服务器 SSH 地址：

```powershell
scp -r <SERVER>:/home/xza_100850113/projects/G2Teleop .
cd G2Teleop
```

建议使用 Python 3.11 或 3.12：

```powershell
py -3.11 -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
py -m pip install -e ".[windows,test]"
py -m pytest -q
```

## 3. Windows 单机只读确认映射

1. F710背面拨到 `X`；接收器只插笔记本；打开手柄电源。
2. `Win+R`输入 `joy.cpl`，确认出现F710且按键/轴响应正常。
3. 在项目根目录运行：

```powershell
py scripts\02_windows_f710_readonly.py
```

逐个测试左右摇杆、LT、RT、十字键、LB、RB。中立预期为摇杆约0、LT/RT约0、hat为
`[0,0]`。若 `rx/ry/lt/rt`对应错误，**不要启动 sender**，保存输出用于修正
`configs/f710_windows_xinput.json`。此程序不开网络。

## 4. 把 receiver 放到 G2

保持F710仍插在笔记本。只复制运行所需的项目文件，不触碰 `g2_robot`或厂商目录：

```powershell
ssh agi@10.130.86.13 "mkdir -p /home/agi/xza/G2Teleop_staging"
scp -r src scripts configs pyproject.toml README.md doc `
  agi@10.130.86.13:/home/agi/xza/G2Teleop_staging/
```

首次用 staging 是为了让你先SSH检查目录；确认后可以将该目录作为正式项目目录。随后SSH进入
G2，由你本人运行纯网络命令：

```bash
cd /home/agi/xza/G2Teleop_staging
PYTHONPATH=src python3 scripts/03_g2_shadow_receiver.py \
  --bind 127.0.0.1 --port 7904 --watchdog-ms 100 \
  --log /home/agi/xza/G2Teleop_staging/g2_shadow_intents.jsonl
```

必须看到 `NO-GDK SHADOW`和 `Listening only on 127.0.0.1:7904`。脚本源码没有
`agibot_gdk`，不创建 `Robot()`，不调用控制接口。这个终端保持打开。

## 5. Windows 建立直达 G2 的 SSH 隧道

打开第二个 PowerShell：

```powershell
.\scripts\04_windows_g2_tunnel.ps1 -G2Host 10.130.86.13 -G2User agi
```

它等价于：

```powershell
ssh.exe -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=2 `
  -o ServerAliveCountMax=2 `
  -L 127.0.0.1:7904:127.0.0.1:7904 agi@10.130.86.13
```

这是“笔记本直接到G2”，不会经过6000D。G2 receiver 必须保持 `127.0.0.1`监听。

## 6. 启动 Windows Shadow sender

打开第三个 PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
py scripts\03_windows_shadow_sender.py --host 127.0.0.1 --port 7904
```

安全操作顺序：

1. 松开所有摇杆、扳机、LB、RB；
2. 十字键左/右选择左臂或右臂；
3. 十字键上选择平移，十字键下选择旋转；
4. 按住RB才产生非零意图；松开RB立即发零输出/HOLD；
5. RT闭合所选手的 `g`，LT张开 `g`；LB为所选手的拇指避障 `c`；
6. 只观察两个终端和JSONL，机器人不应有任何运动。

| 模式 | 左摇杆上下 | 左摇杆左右 | 右摇杆上下 | 右摇杆左右 |
|---|---|---|---|---|
| 平移 | 前后 `vx` | 左右 `vy` | 上下 `vz` | 忽略 |
| 旋转 | pitch `wy` | roll `wx` | 忽略 | yaw `wz` |

当前符号只是候选，必须结合左右 tool frame 做 FK/IK Shadow 后再冻结，不能据此 Powered。

## 7. 真机状态 Live IK Shadow（当前下一步，仍然零输出）

这一阶段会读取真实G2的腰部5D和双臂14D，但只计算虚拟IK候选。脚本
`scripts/10_g2_live_ik_shadow.py`经静态测试不包含`joint_servo_control`、`move_ee_pos`或
`send_action`。它必须是测试期间唯一创建GDK `Robot()`的用户程序；禁止同时运行DM0.5 client、
recovery、servo probe或其他Robot脚本。

### 7.1 现场与进程预检

1. F710接收器保持插在Windows，**绝不能插回G2**；
2. 清空双臂附近工作区，另一人守住物理急停；
3. 旧的`03_g2_shadow_receiver.py`若仍开着，在它自己的终端按`Ctrl-C`；
4. 在G2只读检查：

```bash
ss -ltnp | grep ':7904' || true
pgrep -af 'g2_client|powered|servo|training_data_replay|hand_home|reset_pose|10_g2_live_ik_shadow' || true
```

7904必须无人监听。若第二条发现你启动的DM0.5、恢复或硬件测试程序，回到其原终端正常停止；
不要盲目kill厂商系统服务。无法确认唯一owner时本轮停止。

### 7.2 G2本地启动只读owner

只允许操作者在G2本地终端执行；不要通过服务器SSH代跑：

```bash
bash /home/agi/xza/G2Teleop/scripts/10_g2_live_ik_shadow.sh \
  --bind 127.0.0.1 \
  --port 7904 \
  --watchdog-ms 100 \
  --max-state-read-ms 30 \
  --log /home/agi/xza/G2Teleop/logs/live_ik_shadow.jsonl
```

程序必须先打印`READ-ONLY LIVE IK SHADOW`和三次状态preflight通过，然后才出现
`Listening on 127.0.0.1:7904`。任何missing joint、state read超时、模型哈希或GDK初始化错误都
不要绕过，把完整错误保存下来。

### 7.3 Windows两终端

PowerShell A保持隧道：

```powershell
cd $HOME\G2Teleop
ssh.exe -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=2 `
  -o ServerAliveCountMax=2 `
  -L 127.0.0.1:7904:127.0.0.1:7904 agi@10.130.86.13
```

这里故意直接使用`ssh.exe`，不依赖PowerShell Execution Policy。若要使用仓库的`.ps1`包装脚本，
必须先在同一个PowerShell执行`Set-ExecutionPolicy -Scope Process Bypass`。

PowerShell B使用已经验证过的项目Python环境：

```powershell
cd $HOME\G2Teleop
$env:PYTHONPATH = (Resolve-Path .\src).Path
python .\scripts\03_windows_shadow_sender.py --host 127.0.0.1 --port 7904 --hz 50
```

当前命令显式使用仓库`src`，不依赖之前误建在用户目录的`.venv`或editable install。若提示缺少
`pygame`，只在当前`python`中执行`python -m pip install "pygame>=2.5,<3"`，随后用同一个
`python`重试。

### 7.4 第一轮操作顺序

每次只做一个短脉冲，目标是验证符号，不是扩大虚拟工作空间：

1. 全部控件松开，十字键左、十字键上；
2. 按住RB但不动摇杆2秒，确认`takeover_initialized`；
3. 左摇杆上约20%幅度，持续0.2--0.3秒，松杆，再松RB；
4. 对左摇杆右、右摇杆上分别重复，验证`+X/+Y/+Z`候选；
5. 十字键下切旋转模式，对roll/pitch/yaw分别做约20%幅度短脉冲；
6. RB松开后选右侧，按相同步骤验证右臂；
7. 最后在RB已松开的情况下关闭Windows sender，确认G2记录HOLD/断联。

这一步`LT/RT/LB`只记录人工手意图，尚未生成O10候选。整个过程中真实双臂与手都必须完全不动。
若出现任何真实运动，立即松RB、关闭sender、G2终端`Ctrl-C`、必要时按物理急停；这意味着另有
进程在下发，与Live Shadow代码的预期不符。

测试结束在G2 receiver终端按`Ctrl-C`，确认打印HOLD并执行`gdk_release`。保留日志，随后只读
统计方向、真实关节漂移、GDK state read、ACK RTT、IK耗时和watchdog，未审计前不得Powered。

## 8. 必做故障测试

- RB按住时换侧/换模式：应忽略并打印warning；
- 拔掉F710或关闭电源：sender进入HOLD，不自动恢复；
- 重新连接：v1需重启sender；随后先中立、重新选侧、再产生新的RB上升沿；
- 关闭SSH tunnel：sender报告transport unavailable；receiver约100ms后HOLD；
- 恢复tunnel：必须先收到disabled包，不能带着RB自动恢复；
- 重复/倒序sequence、越界值、未知字段、超长包：receiver拒绝；
- G2执行 `ss -ltnp | grep 7904`，只应显示 `127.0.0.1:7904`。

## 9. 后续边界

Live Shadow通过后才继续左右O10各自`g/c`标定，并接入DM0.5接管状态机。正式硬件输出仍是：
双臂14D走100Hz
`joint_servo_control(normal)`；双手20D走10Hz
`move_ee_pos(group=dual_tool,target_type=o10_t2)`。任何 Powered 测试均需另行明确授权、
现场急停人员和毫米级分阶段验证。
