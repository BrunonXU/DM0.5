# 数据目录

详细字段说明：

- [G2原始数据与H5字段说明](G2_RAW_H5_FIELD_GUIDE.md)

| 目录 | 性质 | 是否可重新生成 |
|---|---|---|
| `raw_samples/` | 私有原始episode样本，不进入Git仓库 | 应从内部存储恢复 |
| `processed/smoke/` | OpenDM JSONL和无重编码MP4 smoke | 可以由转换工具重新生成 |
| `audits/` | 归档索引、H5审计和动态诊断证据 | 建议保留 |
| `manifests/` | 后续冻结的train/validation划分 | 正式生成后必须保留 |

原始数据来自内部共享存储，Git仓库不记录服务器地址或访问凭据。

私有仓库只跟踪`processed/smoke/`中的单episode格式示例。原始H5/H.265和
本地检查样本放在已被Git忽略的`raw_samples/`中；需要时可从共享盘恢复。

不直接从SMB训练；大归档先复制到训练服务器本地NVMe。转换产物不写回共享盘。
DM0.5模型权重不放在本目录。
