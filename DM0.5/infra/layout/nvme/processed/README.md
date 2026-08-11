# 处理后数据

保存通过episode准入检查后生成的训练数据和审计证据。

- `videos/`：三路H.265无重编码封装后的MP4；
- `jsonl/`：显式34D state/action训练与验证JSONL；
- `audits/`：归档级和episode级审计报告；
- `manifests/`：最终有效集合、95/5 split及其hash。

所有产物必须能追溯到源归档、转换配置和项目commit。
