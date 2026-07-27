# G2 数据工具

这组工具只面向第一版 34D absolute-joint DM0.5 基线。

- `audit_episode.py`：完整扫描 H5 的轻量数值与时间戳，使用 `ffprobe`
  检查视频流，但不逐帧解码视频。
- `index_zip_archive.py`：完整读取 ZIP 中每个 `meta_info.json` 和中央目录，
  只检查必需文件是否存在，不读取或解码视频内容。
- `build_split.py`：只接受审计通过的 episode，按 AID、软件版本和 task
  分层，生成确定性的 95/5 episode split。
- `convert_episode.py`：把三路原始 H.265 无重编码封装成 MP4，并输出每帧
  都显式包含 `state` 和 `action` 的 OpenDM JSONL。
- `process_split.py`：按冻结后的 split manifest 批量执行审计和转换；任一
  episode 失败都会记录原因并返回非零状态，禁止静默混入训练集。
- `analyze_dynamics.py`：计算34维逐维范围、唯一命令数，以及分组的
  action→state 最佳响应滞后；用于识别常量维、单位异常和控制语义变化。

H5 对齐帧号不等于原始视频帧号。转换器强制读取三路相机 `.txt` 时间戳，
用 H5 中每帧的 `timestamp/camera/*` 做精确查表；缺少时间戳或无法匹配时
立即失败，禁止退化为按 H5 帧号直接索引视频。

要求在服务器本地 NVMe 上先解压 episode，再运行审计和转换。不要直接从
SMB 共享盘训练，也不要向共享盘写入转换产物。

转换后的目录约定：

```text
g2_sort/
├── jsonl/
│   ├── train/
│   └── validation/
└── videos/
    └── <episode_id>/
        ├── images_head.mp4
        ├── images_left_wrist.mp4
        └── images_right_wrist.mp4
```

运行这些脚本需要 `h5py`、`numpy`、`ffmpeg` 和 `ffprobe`。正式批处理前先对
一个 episode 执行审计、转换和 OpenDM 加载测试。
