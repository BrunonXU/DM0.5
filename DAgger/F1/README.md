# F1 HIL / DAgger

F1 双臂与双夹爪 16D：F710 人工示教、策略接管、三图 30Hz 录制及审核导出。

先读 [当前状态](../docs/F1/STATUS.md)，再按 [三端启动与操作](../docs/F1/HIL_WORKBENCH.md) 使用 4242 的自由接入、人工示教或策略 HIL。稳定旧入口及完整按键说明见 [F1 操作手册](../docs/F1/F1_HG_DAGGER.md)。真实动作需在现场 Enter、手柄 START 后执行。

机器配置在 `configs/`；接入时明确设置 `F1_INFRA_ROOT`、`F1_PROFILE_PATH` 和共用 `F1_CONTROL_LOCK`，模型及数据目录按现场配置。代码在 `src/f1dagger/`，公共组件在 `../shared/`。

开发机从 DAgger 根目录打包：

```bash
python3 F1/scripts/53_package_f1.py --output /tmp/f1-dagger-release.tar.gz
```

[全部 F1 文档](../docs/F1/README.md) · [验证记录](../docs/F1/F1_VALIDATION.md)
