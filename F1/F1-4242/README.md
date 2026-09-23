# F1 4242 具身研发工作台

开发主副本在6000D，本机版本部署于 `/home/user/Bruno/F1/console/`。当前发布`20260923-console-v3`，用户已明确同意启用双臂/夹爪现场手动试用；头/腰/底盘保持关闭。当前功能及验收状态见[根目录功能表](../F1-infra.md)；上肢试用脚本见[Pico说明](../pico_upper/README.md)。

## 打开与开发

- F1局域网：`http://192.168.31.58:4242/`。
- 6000D已经建立 `127.0.0.1:4242 → F1:4242` SSH隧道。VS Code连接6000D后转发4242，在浏览器打开对应本地地址，通常为 `http://localhost:4242/`。
- 6000D的 `f1-console-web-sync.service` 监测本目录`web/`，仅同步网页到F1 `console/dev/web`。浏览器每2秒检查版本，变化自动刷新并保留目标草稿。后端/机器配置须重新发布，不自动热替换动作服务。
- 页面默认只读。需要保存预设或现场调试，在F1 user终端执行 `cd /home/user/Bruno/F1/console/current && ./access.sh`，网页“启用操作”输入访问码。访问码只在本机runtime，未写入代码/URL。
- 已录制的5.82GB样例：打开“数据”页的三路预览入口。它是9月16日的历史图像，和总览的实时相机分开。

当前以独立systemd user临时服务运行，退出IDE不中断；机器重启后需要重新启动。F1本机恢复网页服务（不会启动动作）：

```bash
systemd-run --user --unit=f1-console-4242 --property=Restart=on-failure --property=RestartSec=3 /home/user/Bruno/F1/console/current/run.sh
journalctl --user -u f1-console-4242 -f
```

6000D查看开发同步/SSH隧道：

```bash
systemctl --user status f1-console-web-sync f1-console-f1-tunnel
journalctl --user -u f1-console-web-sync -f
```

6000D独立本地预览（无ROS/动作）：

```bash
cd /home/xza_100850113/projects/F1/console
python3 server.py --offline --host 127.0.0.1 --port 18442 --runtime /tmp/f1-console-preview --control-lock /tmp/f1-console-preview.lock
```

## 已实现与边界

| 功能 | 行为 |
|---|---|
| 状态/相机/磁盘 | 真实HAL/夹爪/底盘/电池；各路接收时间单独判定，不用缺失值填零。磁盘每5秒只读检查 |
| 姿态库 | 保存当前组实测、编辑目标、导入导出JSON；具名关节、机器人身份、范围校验；载入不执行 |
| 手动执行 | 动作ID/结果、实测到位；单次双臂小幅插补和夹爪。机器profile与运行合同核验通过才可用 |
| 控制权 | 本机访问码、浏览器3秒租约、Pico共享flock、DDS endpoint GID排他检查；停止不等于硬件急停 |
| 身体/底盘 | 有实时反馈/目标编辑组件；本机未验收的能力关闭。身体角度不是厘米高度；底盘有外部发布者时不接管 |
| Pico | 展示上肢试用的现场检查/启动/停止脚本；不从网页自动启动 |
| 数据 | 显示现有流程、容量和抽帧预览。不创建录制/传输/配对，不改变原数据 |
| 待接 | URDF关节映射、绝对腰高、全身复位/大幅姿态规划、导航、Demo序列、模型实验、中央数据审阅 |

手动双臂路径是关节插补，没有环境碰撞规划。初版调试范围单次最多5°，5°/s，15°/s²，时长0.25–10秒；这些是初版手动调试配置，**不是硬件能力上限，也不作用于Pico轨迹**。大幅预设可保存但尚未提供执行验收。头/腰反馈与参考限位尚有差异，不用放宽限位掩盖问题。

`robot/runtime_contract.py`启动时核对machine-id、唯一运行控制器及二进制SHA、实际声明的ROS参数；当前mode3/impedance1接受关节位置目标，无需切模式。核验失败保持只读。ROS参数不是SDK物理模式即时ACK；重启驱动或改变控制配置后需重新启动工作台核验。

## 架构与复用

浏览器静态ES模块 → `server.py`（HTTP/认证/状态/显式请求）→ `core.py`（预设、控制租约、动作生命周期、事件）→ `robot/adapter.py`（ROS订阅/惰性发布/反馈确认）。页面不直接调用shell或ROS。

Hall复用的是调姿/预设交互经验，未运行其全套Flask/回放/导航程序；底层复用F1原有控制器、相机及协议。新版Pico独立适配meaxiao1.4.4。录制/传输/审阅在此架构留独立适配位置，后续确定服务器后接入。

版本代码在`releases/<id>/workspace/F1/console`；`current`入口、`runtime`访问码/预设/事件、`dev/web`热开发文件分开。`deploy/release.py`打包选定文件、备份已有部署、核验哈希后切换。升级必须先结束该工作台自己的运行服务；不停止原驱动或其他任务。旧release与6000D部署前备份均保留。

## 验证

- `python3 -m pytest -q tests`：控制合同、STOP/TTL、实际反馈更新、发后失败语义、共享租约及预设原子性。
- Chrome：六页、登录、14/3/5关节输入、离线禁用、草稿刷新保留、390px手机布局；证据在`results/browser/`。
- F1：ROS订阅、运行合同、HTTP页面/状态/图片、隧道和热同步只读验证；本轮没有发送机器人动作。
- 部署、测试通过与现场动作验收分别记录，不能互相替代。
