# G2 × DM0.5 机器人现场项目

这是G2 `/home/agi/DM0.5`在6000D上的开发副本。两台机器已接入公司内网，
6000D修改后通过`scp`直接传对应文件到G2；真机命令仍由Bruno手动运行和验收。

## 从哪里开始

1. [`docs/00_G2_OPERATOR_GUIDE.md`](docs/00_G2_OPERATOR_GUIDE.md)：上机必读。
2. [`docs/Bruno.md`](docs/Bruno.md)：当前进度和下一步。
3. [`docs/G2_ROBOT_STRUCTURE.md`](docs/G2_ROBOT_STRUCTURE.md)：目录职责。
4. [`docs/12_G2_CLIENT_DM05_RUNTIME.md`](docs/12_G2_CLIENT_DM05_RUNTIME.md)：设计合同。
5. [`docs/G2_TROUBLESHOOTING.md`](docs/G2_TROUBLESHOOTING.md)：历史故障。
6. [`docs/GDK_2.6.3_RELEVANT_API.md`](docs/GDK_2.6.3_RELEVANT_API.md)：官方接口摘要。

## 目录

| 路径 | 作用 |
|---|---|
| `g2_sort/config/` | 唯一DM0.5合同和复位姿态 |
| `g2_sort/deployment/` | 正式VLA客户端 |
| `g2_sort/operations/` | 诊断、复位、显式硬件实验 |
| `scripts/` | 现场Bash入口 |
| `tests/` | 无GDK自动测试 |
| `docs/` | 操作、设计、结构、故障和状态 |
| `logs/` | 分类现场产物；根目录不直接堆运行日志 |

查看正式VLA入口帮助，终端执行：

```
python3.12 -m g2_sort.deployment.g2_sdk_client --help
```

现场人员优先使用`scripts/*.sh`，不要直接拼长Python命令。

现场唯一复位入口：

```text
./scripts/03_reset_home.sh          # 只读预览双手20D和双臂14D目标
./scripts/03_reset_home.sh --execute # Split真实复位：先双手、后双臂
```

旧`03_hand_home.sh`和`03_reset_pose.sh`已删除，避免同一编号和功能名称混淆。

各主要代码目录均有自己的`README.md`。新日志固定进入
`logs/{telemetry,diagnostics,gdk_native,bundles}/`对应子目录。
