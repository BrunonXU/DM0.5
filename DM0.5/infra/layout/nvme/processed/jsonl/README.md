# OpenDM JSONL

保存按完整episode划分的`train/`和`validation/` JSONL。

每行必须显式包含三路图像引用、固定prompt、34D measured state和34D
absolute action。禁止用未来state隐式替代action。
