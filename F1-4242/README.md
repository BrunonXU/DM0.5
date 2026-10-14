# F1-infra

F1 机器人工作台：相机与状态、调姿、Pico 遥操、F710 HIL 状态、16D 录制和数据回放审核。独立项目，无需外层 `F1/` 目录。

## 快速开始

解压后进入 `F1-infra/`。先离线看网页（Linux，Python 3.10+）：

```bash
bash run.sh --offline --host 127.0.0.1 --port 18442 \
  --runtime /tmp/f1-infra-preview --control-lock /tmp/f1-infra-preview.lock
```

打开 **http://127.0.0.1:18442/**。

部署到有现成 ROS Jazzy / F1 驱动的机器人，先复制配置：

```bash
cp configs/installation.env.example configs/installation.env
cp configs/robot.example.json configs/robot.json
```

- `installation.env`：设置安装/runtime 路径、`F1_PROFILE_PATH`（指向 `configs/robot.json`）、ROS/DDS 环境；使用遥操时配置 Pico/LXL 路径。
- `robot.json`：填写机器人 IP、machine-id、控制器/监听器哈希、话题、限位和数据目录。模板默认关闭动作，完成本机核验后再启用。

```bash
bash run.sh --host 0.0.0.0 --port 4242
```

打开 **http://机器人IP:4242/**。运动功能还需本机构建监听器、设置控制码并现场验收，步骤见 [部署与换机](docs/SHARING.md)；常驻启动见 [自启](docs/AUTOSTART.md)。

[使用说明](docs/USER_GUIDE.md) · [全身配置](docs/JOINT_CONFIGURATION.md) · [全部文档](docs/README.md) · [版本与已知限制](docs/STATUS.md) · [Agent 工作约定](AGENTS.md)

[HIL 标签页](docs/HIL.md)：自由接入、人工示教、策略HIL三路线；三端指引、可点击手柄教学、彩色状态和三图预览，关闭页面不影响遥操。
