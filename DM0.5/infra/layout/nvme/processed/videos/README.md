# 训练视频

保存头部、左腕和右腕三路RGB的MP4。视频由原始H.265无重编码封装产生，不
预先resize为PNG。

OpenDM按JSONL中的视频路径和`frame_idx`随机定位，在线补边并resize到
448×448。
