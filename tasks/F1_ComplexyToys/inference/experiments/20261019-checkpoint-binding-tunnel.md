# checkpoint切换：F1推理隧道丢失

2026-10-19，切换7000步时失败在第二次绑定回执。根因是6000D到F1的反向推理隧道不存在，F1 localhost:8096拒绝连接；不是控制锁占用或IK问题。

已恢复隧道并完成7000步切换。6项回归通过，10个录制验证观测全部返回有效动作，F1 health与本机模型身份一致；未启动机器人运动。

```mermaid
flowchart LR
  A[检查并恢复缺失隧道] --> B[两端health身份一致]
  B --> C[备用端口模型检查]
  C --> D[持控制锁切服务与配置]
  D --> E[7000步绑定完成]
```

## 原因与证据

出错时6000D服务ready，F1 health报`URLError(ConnectionRefusedError(111, Connection refused))`，F1控制锁空闲。6000D tmux只剩H20的两个W&B会话，没有`f1-toys-policy-tunnel`。隧道何时/为何退出未留有足够日志，不推断为机器人重启。旧代码在F1远端进程EOF时统一提示“a control session may be active”，误导了原因判断。第二次回执失败前已经成功拿到控制锁。

原失败事务已回退4500服务和模型配置，未发机器人动作。7000候选模型本身检查通过。

## 最小修复

`checkpoint.py`复用既有`start_policy_tunnel.sh`恢复缺失隧道，停止旧模型之前核对F1/6000D health完全一致；不强杀现有会话或抢控制锁。远端按control_lock/read_config/tunnel_health/model_identity/write_config/commit返回明确错误。写入配置后异常或未收到COMMIT恢复原配置；旧服务回退机制保留。

只修改部署检查和错误报告，推理模型、ROI、采样、IK、恢复状态机、动作发布逻辑未改。

## 验收

`python3 -m unittest discover -s tasks/F1_ComplexyToys/tests -p test_checkpoint_manager.py -v`：6项通过，覆盖缺隧道不停止服务、控制锁占用、失败服务回退、错误原因保留、远端提交和断开恢复、包校验拒绝。

实际重试输出`ACTIVE checkpoint=7000`。模型ID `f1-toys-eef-57e4ff0c3f5e76f1`，bundle SHA `57e4ff0c3f5e76f14625101fdeaacc42f0e37789ef3fd960510e1865f6c0b1e1`。本机10个录制验证观测10/10有效；远端绑定时F1 health核对通过。日志`artifacts/20261016-v1-formal-training/activate-7000-tunnel-fix.log`，身份回执`active-checkpoint.json`。不将本次零动作验收称为真机任务成功。
