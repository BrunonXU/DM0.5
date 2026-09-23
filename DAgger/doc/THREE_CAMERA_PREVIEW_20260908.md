# 2026-09-08 三路带状态回放

> 历史/专项证据：本文结论对应文内日期与轮次。当前实现及下一步统一见 [当前进度](../RECORDING_AND_PROGRESS.md)，按序查阅 [文档索引](00_INDEX.md)。

用户要求查看上一轮三图拼接及DM0.5/HIL切换。只读核对G2时，最新完整episode为g2_powered_hil_20260908_141409，仍是首次START门控修改前的现场轮次；不可说是新门控验收。

本轮将该episode原生payload与journal复制到开发机同名data/raw目录，离线生成data/exports/g2_powered_hil_20260908_141409/three_camera_preview.mp4。脚本为scripts/42_render_episode_preview.py，依赖Pillow、Noto CJK字体和ffmpeg，不导入GDK或发送控制。

渲染语义：按host available_monotonic_ns取最近已到达的整组三图，1440×570、30fps原速显示；展示帧保持不是增加采集新帧。原数据不裁剪/加速、不插值生成新图；三图按head/left_wrist/right_wrist排列。顶部状态来自journal的ownership事件及control_source，区分DM0.5执行、等待推理、HIL操作/HIL无输入、自动Home、HOLD及保护保持。底部有全程状态条，侧边标源frame_id和host读取后时间。host对齐不代表相机曝光同步。

每个使用的源图在编码前校验长度和SHA-256并解码检查尺寸。输出JSON保存状态区段，JPEG为约30秒处预览图。该视频只用于人工检查，training_ready=false，不是训练视频时间映射认证，也不改变原始数据审计结论。

完成验证：MP4为H.264、1440×570、30fps、3602展示帧、120.067秒、8555135字节（约8.6MB）。ffmpeg完整解码到null无错误；已人工查看30秒预览帧，三路画面/中文标签/时间条可读。状态标签来自真实journal，不推断模型成功与物理跟踪误差。
