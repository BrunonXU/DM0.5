# G2 分拣数据初步审计

## 已确认的归档

共享盘目录包含三个独立归档，不能先验地把它们解释为
train/validation/test：

| 归档 | 字节数 | 当前确认信息 |
|---|---:|---|
| `record.tar.gz` | 50,394,926,225 | 旧设备，软件 2.1.1，task 4 |
| `record_20260409.zip` | 193,356,130,953 | 401 episode；新设备，软件 2.2.0，task 4 |
| `record_20260413.tar.gz` | 215,996,415,546 | 新 AID，软件 2.2.0，task 5 |

`record_20260409.zip` 的元数据全量索引结果是：401 个 episode，其中 371
个元数据有效；367 个同时存在 `aligned_joints.h5`；30 个 600 秒异常录制；
另有 4 个元数据有效但缺 H5。其余两个 tar.gz 归档将在复制到服务器本地
NVMe 后做同样的顺序索引，避免经 SMB 对压缩 tar 做多次全流扫描。

## 单 episode 格式

已确认的 episode 结构包括：

- `meta_info.json`：AID、软件版本、task、duration、integrity 和 validation；
- `record/aligned_joints.h5`：30 Hz 对齐后的逐帧 state/action/timestamp；
- `camera/head_color/head_color.h265`；
- `camera/hand_left_color/hand_left_color.h265`；
- `camera/hand_right_color/hand_right_color.h265`。

H5 顶层是连续的字符串帧号。每帧第一版使用：

- state：`state/joint/position` 14D + 左手 10D + 右手 10D；
- action：`action/joint/position` 14D + 左手 10D + 右手 10D；
- EEF、头、腰和底盘字段仅作诊断。

抽取 episode 的完整 H5 数值检查显示，手臂 action 是绝对目标而非 delta；
state 对命令存在约 2 帧响应滞后，手指约 14～16 帧。正式批处理仍会对每个
有效 episode 的完整 H5 执行 NaN/Inf、范围、连续帧号和时间戳检查，但不会
无目的地逐帧解码所有视频。

对一个真实完整 episode `sorting_19700101095605` 的进一步验证发现：

- H5 有 977 个对齐帧；
- 头部原始视频有 1004 帧，H5 使用原始帧 1～977；
- 左右腕原始视频各有 1007 帧，H5 使用原始帧 4～980；
- 头部对齐后有963个唯一画面，左腕858、右腕819；腕部重复比例较高。
  当前记为质量 warning 而非直接判坏，正式全量审计会比较它是否是稳定的
  采集对齐特征，还是仅出现在部分session。

因此 H5 顶层帧号不能直接作为视频 `frame_idx`。转换工具现在强制读取每路
相机 `.txt`，用 H5 的 `timestamp/camera/*` 精确映射原始视频帧号；缺少
时间戳或无法匹配会直接排除该 episode。

三路对齐画面实查：

- 头部为 `640×400`，完整覆盖桌面、两个托盘、两瓶和双臂；
- 左右腕均为 `1280×1056`，有明显广角畸变和机械臂自身遮挡，但不存在需要
  立即裁掉的固定黑边或损坏区域；
- 三路视角互补，第一版继续采用官方补边后 resize 到 `448×448`，不主动
  裁边。若后续发现固定坏点/隐私区，再把裁剪作为有对照实验的第二版变更。
- 完整episode首尾核对：开始时雪碧和橙汁位于左侧纸板区域，右侧白色托盘
  为空；结束时两瓶均进入白色托盘，雪碧在托盘左侧、橙汁在托盘右侧。
  当前固定英文prompt的目标方向与这一真实episode一致。

同一真实 episode 的 action→state 动态诊断：

- 左臂和右臂的最小平均绝对误差均出现在命令后2帧；
- 左手约17帧，右手约19帧，符合灵巧手反馈明显慢于手臂的现象；
- 少数手指 abduction 维在该 episode 内恒定或近似恒定。当前不能仅凭一条
  数据删除这些维度：正式全量审计会统计每维范围和唯一值数量，34D接口保持
  不变，常量维由norm stats和action mask合同正常保留。

## 尚未冻结的兼容性结论

- 旧 AID/2.1.1 是否能与新 AID/2.2.0 合并，必须根据全量 34D 顺序、范围和
  控制语义审计决定，不能只凭任务视频相似就合并。
- task 4 与 task 5 的文本标签不同，但已查看的片段都表现为同一双瓶完整
  流程；正式转换前要用完整元数据索引和流程抽查确认。
- 单位暂时原样保留。训练不需要先改单位，但 powered motion 前必须与 G2
  SDK 的 joint index、单位和命令频率逐项核对。
