# G2部署入口

正式入口：

```bash
python3 -m g2_sort.deployment.g2_sdk_client --help
```

默认`--mode shadow`，完整请求DM0.5但不创建servo发送器。通过shadow后先用
`powered-sync`短前缀验证，`powered-async`必须显式选择。

实现位于`g2_client/`。相机、state、servo和调度不依赖模型协议；新模型只新增
一个实现`PolicyBackend`并返回固定`ActionChunk`的后端。第一版唯一启用的后端
是DM0.5 multipart HTTP，客户端只配置endpoint，不配置checkpoint。

控制时序原理见
[`docs/robot/RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md`](../../docs/robot/RTC_AND_CONTROL_TIMING_FIRST_PRINCIPLES.md)，
目标进程边界见
[`docs/robot/RTC_PROCESS_ISOLATION.md`](../../docs/robot/RTC_PROCESS_ISOLATION.md)。
现场命令以 `g2_robot/docs/00_G2_OPERATOR_GUIDE.md` 为准。
