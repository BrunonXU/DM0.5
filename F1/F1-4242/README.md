# F1-4242 · 具身研发工作台

面向 F1-09 的相机、姿态调试、Pico 状态和数据审阅入口。开发主副本统一放在本目录；机器人端独立部署于 `/home/user/Bruno/F1/F1-4242/`。旧本地 `F1/console` 是兼容链接，不再维护第二份源码。

- 局域网打开 **http://192.168.31.58:4242/**。
- VS Code 连接 6000D 并转发 4242 后，打开 **http://localhost:4242/**。
- [使用说明](docs/USER_GUIDE.md)：接管、调姿、相机、数据审核与常见问题。
- [接口文档](docs/API.md)：HTTP 请求、字段、单位、认证与错误行为。
- [架构说明](docs/ARCHITECTURE.md)：模块职责、复用来源、实时状态与部署。
- [模型说明](docs/MODEL.md)：URDF 来源、关节映射与显示边界。
- [完整 F1 基础能力](../F1-infra.md)与[Pico 上肢遥操](../pico_upper/README.md)。

## 当前版本

0.2.0 / `20260923-f1-4242-v4`：米白黑色界面、相机置顶、实时值初始化、滑条目标、控制码接管、URDF、数据目录及审核。发布/实机检查结果见 `results/deployments/` 与 `results/redesign_20260923/`；页面和接口验收不等于实物动作验收。

双臂和夹爪沿用用户已批准的现场试用配置。头部、身体、底盘只读；大幅姿态和全身复位未验收。滑条只修改目标预览，点击执行并确认才请求动作。已有动作合同、租约、Pico 互斥及反馈检查继续生效。

数据审核写入本工作台 runtime；原始 MCAP 保持原样。此版本不切换录制器、不自动上传、不配置中央服务器。

## 目录

```text
F1-4242/
  web/              HTML/CSS/JS、离线 3D 依赖与模型
  server.py         HTTP、会话与 API
  core.py           预设、控制租约、动作与事件
  robot/            ROS 反馈、动作适配和运行合同
  configs/          F1-09 配置
  scripts/          热更新、运维工具
  deploy/           带清单的版本发布
  docs/             人类使用说明、接口与架构
  tests/            控制及数据服务回归
  results/          发布备份、测试与页面验收
```

## 开发与运行

6000D 本地离线预览，无 ROS 动作：

```bash
cd /home/xza_100850113/projects/F1/F1-4242
python3 server.py --offline --host 127.0.0.1 --port 18442 \
  --runtime /tmp/f1-4242-preview --control-lock /tmp/f1-4242-preview.lock
```

F1 本地终端启动网页服务（仅启动状态订阅，不执行机器人动作）：

```bash
systemd-run --user --unit=f1-console-4242 \
  --property=Restart=on-failure --property=RestartSec=3 \
  /home/user/Bruno/F1/F1-4242/current/run.sh
journalctl --user -u f1-console-4242 -f
```

已有同名单元运行时不重复启动。该单元为临时服务，重启机器后需恢复；基础 ROS/相机驱动沿用已有现场启动流程。

6000D 的 `f1-console-f1-tunnel` 提供本地 4242 转发，`f1-console-web-sync` 监测本目录 `web/` 并增量同步到 F1 的 `F1-4242/dev/web`。浏览器自动检测前端版本；刷新后不会重新取得控制权或执行目标。后端与配置修改必须重新发布，不能只靠网页热更新。

```bash
systemctl --user status f1-console-web-sync f1-console-f1-tunnel
python3 -m pytest -q tests
```

发布前读取 [F1 工作约定](../AGENTS.md)。`deploy/release.py F1-4242 --release <新版本>` 生成源码清单、上传并校验；已有目标升级需要工作台空闲、停止自有服务和部署前备份。不得停止厂商控制器、Pico 或其他任务以强行接管。
