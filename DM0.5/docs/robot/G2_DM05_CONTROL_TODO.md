---
doc_type: status
domain: g2_control
status: frozen
created: 2026-09-07
updated: 2026-09-14
models: [dm05, new_lora]
hardware: [rtx6000d, g2]
canonical_for: []
supersedes: []
superseded_by: []
related: [../CURRENT_STATUS.md]
---

# G2 / DM0.5 控制链 TODO

> 2026-09-14整理说明：正文保留2026-09-07的待办/交接快照，其中“单34D未验证”和部分名称处理要求已过时。
> 2026-09-08的单34D工作基线及证据以[控制根因文档第0节](G2_SMOOTH_TELEOP_ROOT_CAUSE_AND_NEXT_STEPS.md)为准；现场命令见[操作指南](../../../g2_robot/docs/00_G2_OPERATOR_GUIDE.md)。
> 本次仅归位和标注历史状态，不代表重新执行或验收文中的待办。

> 记录日期：2026-09-07  
> 当前可回退基线：左右各一次动态名称17D、100Hz
> `joint_servo_control(normal)`；DM0.5同步推理现场已确认丝滑。  
> 权威背景：`DM0.5/docs/robot/G2_SMOOTH_TELEOP_ROOT_CAUSE_AND_NEXT_STEPS.md`

## 1. 确认最终Servo请求布局：17D + 17D还是单次34D

> 2026-09-07文档核对：三个总体事项仍待办；右手名称解析问题已定位并完成四指单轴验证，完整Sync复验优先。交接见[G2_DM05_NEW_CHAT_PROMPT.md](G2_DM05_NEW_CHAT_PROMPT.md)。
> 静态hold前仍需处理既有O10零反馈事故的有效性问题，不能把新鲜时间戳等同于手型数值可信。

- [x] 定位右食指尾空格被运动控制端拒绝；canonical名称的右食/中/无名/小指单轴真机验证完成。
- [ ] 名称修正后的入口19完整Sync抓取复验；保留现有双17D、C2与进程隔离，确认右手闭合和第二次抓取配合。
- [ ] 从同一次`get_end_state()`构造动态34D候选：原始名留证据，trim并校验官方已知集合后发送，名称/值严格配对。
- [ ] 先验证单次34D当前位置静态hold，再验证单指0.20rad往返，最后进行相同模型输出的在线A/B。
- [ ] 对比双17D与单34D的SDK调用P50/P95/max、100Hz dispatch gap、左右侧提交偏斜、错误/阻塞和现场观感。
- [ ] 只有单34D无运动控制接收端`JointPositionServo joint name ... not found`、逐轴对应正确、无长阻塞且整体不差于双17D，才把它升级为最终方案；否则双17D保持最终基线。

## 2. 新Servo接口下重新评估OTG、Ruckig、C2和平滑/滤波实验

- [ ] 梳理旧实验中哪些只作用于双臂command生成，哪些结论受到旧O10 `move_ee_pos`执行链污染。
- [ ] 双臂arms-only得到的C2/插值结论保留为参考，但所有“完整双臂+双手”的主观和反馈结论需要在新Servo出口下重新A/B。
- [ ] 固定同一模型输出和同一Servo通道，分别复测raw、C2、OTG/Ruckig及已有平滑/滤波候选；一次只改变一个变量。
- [ ] 同时评价任务成功率、轨迹延迟、command/feedback误差、dispatch稳定性和现场丝滑度，不能只看jerk或单一离线指标。
- [ ] 在新基线已经丝滑的前提下，不默认继续叠加滤波；只有指标或任务效果明确改善才进入正式配置。

## 3. RTC迁移到连续双侧Servo接口并重新验证

- [ ] 把RTC硬件出口从O10 `move_ee_pos`切换为当前动态双17D Servo基线；若单34D之后验证通过，再追加单34D候选。
- [ ] 保持RTC模型输出、预测延迟`d`和执行窗口`s`可追踪，先Shadow确认合同，再做固定短时Powered。
- [ ] 重新测量新接口下的端到端observation age、重规划边界、双臂/双手seam、100Hz dispatch和GDK调用耗时。
- [ ] 重新标定RTC的`d/s`，不能直接沿用旧Split `move_ee_pos`链路的参数和结论。
- [ ] Sync双17D保持可回退基线；RTC完成有限时长验收前，不进入`--continuous`。
