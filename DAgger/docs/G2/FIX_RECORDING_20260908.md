# 2026-09-08 记录合同与30Hz采集修复

> 历史/专项证据：本文结论对应文内日期与轮次。当前实现及下一步统一见 [当前进度](RECORDING_AND_PROGRESS.md)，按序查阅 [文档索引](00_INDEX.md)。

本轮为代码修复、离线测试和旧数据只读审计；未启动GDK/真机。冻结G2Teleop F710入口未改。

## 频率结论

新Powered入口40默认`--capture-hz 30`，共享采集线程持续获取三图/state，HIL暂停推理时仍采集。推理请求频率保持同步整chunk模式；30Hz是动作waypoint/训练行时间合同，不是30次推理请求/秒。Servo仍100Hz；人类手部渐进点目标仍5Hz。

核对依据：DM0.5的data_tools/convert_episode.py按aligned_joints.h5行导出，按各相机原始时间戳匹配帧，视频默认30fps；BuildActionChunk按连续50行取动作，不按时间戳自动重采样；g2_client/contracts.py定义MODEL_HZ=30。未取得原始训练H5逐帧测量，不能把代码默认值当作所有原始数据已达标的证据。

新增41_audit_capture_rate.py：报告host采集频率、三路独立新帧频率、重复/倒退、最大间隔和原始时间戳单位/时钟域；不做跨时钟相减。默认平均频率诊断容差5%，不是训练质量或同步认证。检查旧155216轮769组观测：三路约9.94Hz，30Hz诊断不通过。报告位于data/archive/offline/20260908_capture_rate_155216.json。未改写旧数据，也未用复制帧伪造30Hz。

## 已修

- SDK事件和control嵌套记录统一interface、finished时间、动态名称映射的34D索引、实际提交分量；保留原始API字段和返回值。
- 100Hz插值增加原始waypoint、上下索引、alpha、chunk起点和提交目标anchor。审计据此重建，避免把插值误判成原始waypoint不一致。
- fresh_policy_to_policy明确记录真实请求ID；关联新观测、epoch和响应。
- 双臂/手人工组合source与mask统一；Home单独记录为自动复位，不作为人工纠正标签；policy等待使用policy_wait。
- 无输入保持最后提交目标；Home结束从最后提交手目标重建调度状态。SDK返回、提交目标均不声称是measured。
- POLICY也更新Servo间隔统计，避免跨整个POLICY段计算虚假的人工发送长间隔。

## 本轮验证与剩余

本地全套DAgger离线测试：54 passed。新增实际producer→journal→auditor集成测试覆盖策略插值、组合人工、Home、HOLD、新观测恢复；篡改插值或SDK提交被拒绝。频率测试覆盖30Hz新帧、10Hz、重复帧、缺相机和时钟域变化。

这不是新的Powered验收。下一次现场运行需验证30Hz是否实际达到，尤其相机重复帧、共享SDK锁对100Hz控制的影响和磁盘/内存压力。严格视频解码、时钟对齐、34D反馈有效性、标签筛选和训练导出仍待完成；training_ready=false。

prebuffer-record仍为首次START前约4秒（受内存容量限制）+首次START后直到进程结束；不是退出HIL就自动停录。本轮未改持久化窗口/RTC。

现场启动和结束后审计：

```bash
cd /home/user/xza/DAgger
bash scripts/40_g2_powered_hil.sh --capture-hz 30 --recording-mode hil-record
# 结束后，将下面路径替换为本次打印的Episode路径下journal目录
python3 scripts/41_audit_capture_rate.py logs/powered_hil/本次Episode/journal
python3 scripts/34_audit_hil_episode.py logs/powered_hil/本次Episode/journal
```

沿用START_HERE的Windows隧道及sender；现场ENTER确认。日志：logs/powered_hil_console.log、logs/telemetry/powered/、logs/powered_hil/。为验收完整时间轴这里选择hil-record全程保存，验收后可选prebuffer-record。

### G2同步验证

本轮变更已同步至/home/user/xza/DAgger。G2离线全套测试54 passed（3.83s）；补齐与启动脚本一致的DM0.5 PYTHONPATH后files-only预检通过，明确未导入或初始化GDK。首次手工预检因缺g2_client搜索路径失败，属于该验证命令环境遗漏，不是Powered运行失败。未运行Powered。
