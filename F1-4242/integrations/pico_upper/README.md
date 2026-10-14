# 可选上肢 Pico

基于 meaxiao 1.4.4，提供双臂和夹爪遥操；不控制身体/底盘。工作台可从 Pico 页准备、确认和停止本会话。

先在工作台 `configs/installation.env` 配置 Pico 的 Python、XR SDK、ROS 和共用控制锁，再核对本目录 `config/control.json` 与 `teleop/config.json` 的脚本哈希。

在机器人现场终端，从本目录运行：

```bash
bash check.sh --dependencies  # 仅检查依赖
bash check.sh                 # 只读预检
bash start.sh                 # 现场确认后启动
# 另一现场终端：
bash stop.sh
```

PCService 需已运行；依赖准备、按钮含义、日志与停止边界见 [现场操作](docs/OPERATIONS.md)。新机须重新验证模型、限位、夹爪方向及机器合同。

[适配设计](docs/adaptation.md) · [上游来源](UPSTREAM.json) · [工作台部署](../../docs/SHARING.md) · [网页启停](../../docs/PICO.md)
