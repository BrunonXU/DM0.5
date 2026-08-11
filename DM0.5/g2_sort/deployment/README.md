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

完整合同、命令、时钟和失败矩阵见
[`docs/12_G2_CLIENT_DM05_RUNTIME.md`](../../docs/12_G2_CLIENT_DM05_RUNTIME.md)。
