# 原数据主视角下载（2026-09-09）

用户请求查看数采抓取视频。源文件为/mnt/dm05_nvme/dm05/processed/full/videos/{episode}/images_head.mp4。

- sorting_20260409161901：当前43回放的冻结episode对应主视角，31.734秒，H.264副本14225766字节。视频含原始帧，与948个30Hz action区间31.567秒不是相同计数，需frame_idx映射才能精确关联。
- sorting_20260403151631：此前视觉抽查过的雪碧/橙汁成功交接示范，40.034秒，H.264副本17620876字节；不是当前43的源episode。

只转码原HEVC为H.264/yuv420p/faststart（crf18），原速、不裁剪；未改原文件。两条副本全视频解码通过。
本地及G2相同相对路径：data/exports/dataset_reference/{episode}_head_h264.mp4。G2绝对根/home/user/xza/DAgger。
Windows下载使用实际Desktop系统目录，由用户执行scp；没有声称已写入Windows桌面。
