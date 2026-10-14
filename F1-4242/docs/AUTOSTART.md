# 自动启动与资源边界

本文命令以新装 `/home/user/Bruno/F1-infra/` 为例；实际安装根由 `F1_INFRA_HOME` 配置。F1-09 原部署仍在旧位置，见 [STATUS](STATUS.md)。

## 运行方式

完成安装并启用用户 Linger 后，开机由对应账号的持久 systemd 服务运行工作台。局域网联网后访问 `http://机器人IP:4242/`，不依赖 VS Code 窗口。关机、断电或系统休眠期间无法提供网页；若只关闭电机/驱动，网页可以继续查看目录、历史数据和离线状态。

`f1-console-4242.service` 使用 `WantedBy=default.target`、`Restart=always`、5秒恢复间隔，用户 `Linger=yes` 使用户管理器随开机运行。Linger 的语义见[systemd官方说明](https://github.com/systemd/systemd/blob/main/man/loginctl.xml)。`systemctl stop` 为显式停用，不会被 Restart=always 立即重新拉起；下次开机会按 enabled 设置运行。

自启只启动 HTTP 和反馈订阅，不启动基础驱动、不启动Pico、不开始录制、不执行机器人动作。基础驱动晚启动时，每10秒重试同一机器/二进制/参数合同；失败继续只读，成功只恢复配置许可，仍需登录/租约/显式动作请求。初始合同通过后不持续重查模式，不将它当作电机实时模式ACK。持有操作租约时推迟切换能力。

## 首次安装及维护

安装器按 `--home`、`--profile`、runtime、锁和端口生成服务单元，模板见 `deploy/systemd/f1-console-4242.service`。安装副本在 `$F1_INFRA_HOME/systemd/`，systemd 用户目录保留注册链接。先按 [部署说明](SHARING.md) 激活 `current`、准备其中的本机配置，并确保 `loginctl show-user "$USER" -p Linger` 为 `yes`；需要时由有权限的本机账号执行 `loginctl enable-linger "$USER"`。在机器人对应用户的终端执行：

```bash
cd /home/user/Bruno/F1-infra/current
HERE="$PWD"
source configs/installation.env
python3 deploy/install_service.py --home "$F1_INFRA_HOME" \
  --profile "$F1_PROFILE_PATH" --port "$F1_CONSOLE_PORT"
systemctl --user is-enabled f1-console-4242
systemctl --user is-active f1-console-4242
```

安装器核对机器身份、Linger、共享控制锁及录制/动作空闲，备份实际旧单元到 `runtime/service_backups/<时间>/`，再安装本项目单元。固定服务名仍为 `f1-console-4242`，不可用同一账号同时覆盖另一套正在运行的该服务。已有不同自定义用户单元会先保存证据并拒绝覆盖。不会重启机器人来验收自启。

日常无需命令；需要维护时，在F1 `user` 终端使用：

```bash
systemctl --user status f1-console-4242 --no-pager
journalctl --user -u f1-console-4242 -f
systemctl --user start f1-console-4242
```

停止/重启网页服务要先结束本工作台动作和录制，因为其录制子进程也属于服务进程组。关闭自启使用 `systemctl --user disable f1-console-4242`，它不自动停止当前任务；无需改动原有 `f1-6000d-tunnel` 或其他驱动服务。

远程使用 `localhost:4242` 还需自行配置 SSH / VS Code 端口转发；源码包不包含原 6000D 隧道服务或连接凭据。

## 对数采和推理的影响

| 场景 | 主要工作与影响 |
|---|---|
| 网页常驻 | Python/ROS订阅、保留每路最新JPEG、状态检查；无模型加载、无GPU计算，也不保存相机历史，但仍消耗CPU/内存和DDS接收带宽 |
| 看实时画面/URDF | 服务发送已有JPEG；模型渲染与图片显示在浏览器所在机器。用户开多个页面会增加HTTP/网络负担 |
| 历史三路回放 | 读取已有MP4，经HTTP发送；浏览器解码。派生视频另行生成，不是每次打开页面重新编码 |
| 独立LeRobot录制 | 10fps，三路CPU libx264/veryfast，每路编码线程参数2；另有同步、JPEG处理、Parquet、观测保留和磁盘写入。六个编码线程不代表固定占用六个CPU核，解码/管道等还有其他线程 |
| 执行网页动作 | 命令发布和反馈判定；通过共享控制锁与Pico互斥。其他推理执行器必须遵守同一控制权合同，不能同时抢同一组关节 |
| 与推理并行 | 未使用CUDA不等于完全不影响GPU推理：Thor共享内存带宽、CPU预处理/调度、存储和网络仍可能争用，需要同模型同任务实测 |

当前不设置硬CPU/内存限额或任意降频，避免未经测量造成命令抖动、掉帧或录制被杀。没有“网页可常驻就保证录制零影响”的承诺，也不在用户动作期间启动压力测试。

## 只读资源测量

F1 `user` 终端，在网页运行时执行（只采样，不启动录制/推理）：

```bash
cd /home/user/Bruno/F1-infra/current
python3 scripts/probe_resources.py --seconds 15
```

报告包括整个工作台服务进程组（含已启动的录制器/编码器）的CPU、内存及可用IO计数；CPU 100%表示一个逻辑核，RSS之和可能重复统计共享页，cgroup内存还包含文件缓存。必须连同相机是否有流、浏览器是否打开、是否录制一起解释。

并行验收按同一真实任务比较：主应用单独运行、加网页预览、再加录制。观察推理延迟P50/P95、控制周期/丢帧、录像覆盖、磁盘写入和内存峰值。实际录制与动作由现场操作者启动；空闲读数不能替代该验收。


## v15 开机网卡晚就绪恢复（2026-09-28）

v14 只在 ROS 订阅已创建但电机合同未通过时重试；如果开机时 CycloneDDS 绑定网卡尚未可用，适配器退化成 OfflineAdapter 后没有启动重试线程。网页在线而相机/关节一直为空即由此触发。

v15 同时覆盖初次 ROS 节点创建失败：每10秒重新尝试，只建立反馈订阅与命令来源观察，不接管、不重放动作、不启动基础驱动/录制/遥操。验证失败仍只读；成功后同步更新动作管理器的适配器引用。检查期间有操控租约、动作或关闭请求时不切换适配器，释放新建资源。

原电机节点默认使用 Fast DDS，本次实测 CycloneDDS 参数服务超时而 Fast DDS 读取成功。增加显式 `controller_parameter_rmw: rmw_fastrtps_cpp`，使用独立且限时的只读参数子进程；反馈/动作仍用原 CycloneDDS。子进程超时、非法响应、参数不匹配或读取期间电机进程发生变化都保持只读，不修改原节点环境或参数。

v15修复阶段的记录在 `results/startup_recovery_20260928/`；当时没有重启机器人或原驱动，只核验了失败后的恢复路径。随后同日发生了一次真实整机重启，独立证据如下。


## 实际整机重启后的自启观察（2026-09-28）

本次已观察到F1-09整机重启后，**4242工作台随开机恢复运行**，无需重新打开VS Code或由开发者执行启动命令。前一次boot ID为`df2b3a5c-2077-476f-8333-c6ab122841f8`，这次为`a7a60c44-4759-411e-963e-e2c7bff8c289`，首次复核时系统仅运行115.68秒，确认不是仅网页进程重启。

- `f1-console-4242`于北京时间15:27:00进入运行态，MainPID为2517、NRestarts为0；后续只读复核仍为enabled、active/running，同一PID。
- 工作台线上版本为0.6.4/v20，原生驱动启动后运行合同恢复通过。重启后的头J2反馈为−24.415821°，在现役命令范围内；这只是该时刻的位置读数，不表示工作台开机自动归零或恢复上次姿态。
- 本次证据验证的是工作台常驻服务。motor_ctl进程约在本次开机59秒后开始运行，可能来自现场手动start_all；没有据此确认厂商基础驱动也已设置或完成自启。

这一真实重启观察补充了此前“服务已enabled但未重启实测”的缺口，但不覆盖所有网络延迟、断电顺序与原生服务启动组合。自启仍只建立网页和只读反馈能力，不发姿态命令，也不自动开始遥操、录制或基础启动。

私有证据摘要位于开发目录`results/pose_completion_20260928/autostart-reboot-summary.json`，源记录为`results/head_recovery_20260928/recheck-after-disconnect.json`；未随共享源码包发布。
