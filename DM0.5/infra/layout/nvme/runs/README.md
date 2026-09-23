# 运行记录

每次训练、基准或推理验证使用独立子目录，保存：

- 项目commit和工作树状态；
- 完整命令与配置；
- GPU/world size/global batch；
- 数据manifest及hash；
- loss、吞吐、显存和dataloader指标；
- checkpoint保存/恢复耗时；
- 日志和评估结论。

禁止多个不同合同的实验共用同一运行目录。
