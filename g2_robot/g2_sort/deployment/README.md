# G2正式部署代码

直接启动正式入口时，终端执行：

```
python3.12 -m g2_sort.deployment.g2_sdk_client
```

`g2_sdk_client.py`只转发到`g2_client/app.py`。`g2_client/`内部职责：

- CLI和生命周期：`cli.py`、`app.py`、`cleanup.py`；
- 合同和HTTP：`contracts.py`、`policy.py`、`dm05_backend.py`；
- 观测：`cameras.py`、`robot_state.py`、`observation.py`；
- 动作：`joint_layout.py`、`robot_control.py`、`servo.py`；
- 调度：`interpolation.py`、`control_loop.py`、`prefetch.py`；
- 运维：`keyboard.py`、`telemetry.py`、`mock.py`。

本目录不再放`g2_test_*`、smoke、复位或离线诊断。现场入口见项目根目录
`scripts/`，操作说明见`docs/00_G2_OPERATOR_GUIDE.md`。
