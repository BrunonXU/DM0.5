---
doc_type: experiment
domain: transport
status: active
created: 2026-08-24
updated: 2026-08-24
models: [new_lora]
hardware: [g2, rtx6000d]
canonical_for: [process_isolation_offline_evidence, image_transport_pixel_ab]
supersedes: []
superseded_by: []
related: [../RTC_PROCESS_ISOLATION.md, ../TRANSPORT_BINARY_WEBSOCKET.md]
---

# 单Robot-owner隔离与binary transport离线门禁

## 1. 问题与假设

假设A：Servo卡顿主要来自同一Python进程内Camera/JPEG/HTTP工作造成的调度干扰；把相同工作移到spawn进程后，即使总工作量不变，100Hz线程的interval尾部也会收紧。

假设B：GDK已给JPEG时，删除G2侧decode→JPEG95可同时减少CPU成本和第二代JPEG误差；但模型输入不再与旧链路逐像素相同，必须分像素门和动作门验证。

假设C：50×34动作用float32二进制返回能删除list/JSON工作，而量化误差远低于控制尺度。

## 2. 冻结条件

进程隔离A/B使用同一台无G2 Linux主机、同一个100Hz`ServoStreamer`、三路720p deterministic MockCamera、Pillow JPEG和本地HTTP workload，各15次请求。唯一变量是Camera/JPEG/HTTP工作在Servo同进程还是spawn Policy进程；负载总耗时分别为0.702s和0.704s。

图像A/B使用`g2_robot/logs/diagnostics/preflight_20260817_170042`的头、左腕、右腕真实预检JPEG；冻结服务端预处理为constant-zero PadToSquare和OpenCV INTER_LINEAR Resize448。每候选运行10次CPU计时。预检文件是RGB解码后另存的JPEG95，不是原生GDK bytes，因此只证明再增加一代JPEG的代价；实际native payload仍待下次shadow采集。

## 3. Servo隔离结果

| interval指标 | 同进程 | spawn隔离 | 变化 |
|---|---:|---:|---:|
| mean频率 | 94.957Hz | 99.345Hz | +4.388Hz |
| P50 | 10.061ms | 10.060ms | 基本不变 |
| P95 | 12.429ms | 10.066ms | -2.363ms |
| P99 | 13.701ms | 10.219ms | -3.482ms |
| max | 14.547ms | 10.542ms | -4.005ms |
| `>15ms` | 0 | 0 | 本次负载均未越线 |

负载用时不变而Servo尾部下降，支持“隔离调度干扰”而不是“偷偷减少工作”的解释。Mock state ring共读16次、失败0，单次最大约18µs。

它不能证明G2已达100Hz：测试没有GDK、DDS、Camera硬件、Robot send调用和G2内核负载。真机结论仍需真实发送timestamp。

## 4. 图像候选结果

| 候选 | head字节倍率 | 左腕倍率 | 右腕倍率 | 最终448像素差异 |
|---|---:|---:|---:|---|
| 归档JPEG直接透传 | 1.000 | 1.000 | 1.000 | reference |
| 旧decode→JPEG95 | 1.002 | 1.006 | 1.009 | MAE 0.029～0.050；2.7%～4.3%值变化 |
| resize448 PNG | 2.351 | 0.951 | 1.029 | 三路逐像素exact |
| resize448 JPEG95 | 0.610 | 0.205 | 0.213 | MAE 0.69～1.02；41%～55%值变化 |

G2侧测试处理时间：raw为0；旧二次JPEG三路约15.6ms；resize PNG三路约104.7ms；resize JPEG三路约12.3ms。绝对时间来自本地镜像CPU，不能直接替代G2计时，但足以否定“PNG等价resize必然更快”。

决策：下一次G2 shadow先保存native JPEG metadata/bytes，再测native passthrough；不采用robot resize PNG。resize JPEG保留为未来带宽候选，但必须通过同seed模型动作门。

## 5. binary/WebSocket合同结果

- G2完整无硬件测试：111 passed；
- DM0.5 binary协议测试：3 passed；
- localhost真实WebSocket：连续两个binary请求只建立一个connection，1 passed；
- action payload固定`50×34×4=6800` bytes；协议响应含metadata仍小于7KB；
- 覆盖[-2,2]动作范围的float32最大量化误差低于`1.2e-7`；
- G2和DM0.5请求编码的golden payload长度208 bytes、SHA256一致。

这些测试证明协议、持久连接和数值合同，不证明6000D实际E2E改善，也不证明任务成功率。

## 6. 未完成门禁

1. 在6000D用`evaluate_image_transport_actions.py`跑五步compile、同seed的raw/旧二次JPEG/resize PNG/resize JPEG动作差异。
2. 部署独立WebSocket候选端口，旧7892/7893保持不动；同归档交替请求至少30轮，核对non-model账本和长尾。
3. 有新E2E分布后生成RTC `d/s`候选，再跑现有RTC离线轨迹分组；不能因平均延迟下降就直接把d设为均值对应步数。
4. 下一次连接G2先shadow/preflight，再做短Powered；所有硬件结论仍以telemetry为准。

## 7. RTC标定工具的旧数据sanity check

新增`g2_sort.validation.calibrate_rtc_schedule`，把`observation_age_seconds`先转换为30Hz保守真实步数，再分别报告：`d`覆盖率、早到缓存、每个合法`s`的`H-s`窗口miss率。工具只做延迟筛选，不代替轨迹质量选择。

用5份旧transport RTC日志的270个action chunk试跑：age mean/P99/max为0.538/0.553/0.567s，真实延迟步数P99=17、max=18；若目标是P99再加1步margin，延迟单独会给出`d=18`。但旧固定seed轨迹实验选择过`d=16,s=20`，说明`d`不仅是“ceil平均延迟”：条件去噪质量、晚到时实际switch和`s`窗口必须一起A/B。该18不是新默认，只是工具正确性的旧数据sanity check；WebSocket上线后必须用新分布重算。
