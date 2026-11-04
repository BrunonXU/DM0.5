# G2 Book Demo

本目录保存新任务的任务级配置、文档、评测与数据入口。

- `data/`：从 G2 采集机同步的原始 episode；默认不进入 Git。
- 原始录像保持原格式，转换后的 MP4 放在对应 episode 内的 `mp4/` 子目录。

拉取 G2 `/data/record` 中最新两条：

```bash
./scripts/pull_latest_records.sh 2
```

默认地址来自 `g2_robot` 文档：`agi@192.0.2.8`。脚本只读远端、拒绝覆盖本地同名记录。
