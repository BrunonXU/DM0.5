---
doc_type: reference
domain: data
status: frozen
created: 2026-07-28
updated: 2026-07-28
models: [dm05, old_lora]
hardware: []
canonical_for: [original_g2_dataset_audit]
supersedes: []
superseded_by: []
related: [../core/TRAINING_AND_DEPLOYMENT_CONTRACT.md]
---

# G2 分拣数据审计

更新时间：2026-07-28

## 1. 文档职责

本文只保存数据来源、审计证据、兼容性结论和最终统计。训练字段如何使用由
`../core/TRAINING_AND_DEPLOYMENT_CONTRACT.md`冻结；逐维顺序见
`g2_sort/config/action_contract.json`。

## 2. 原始归档

| 归档 | 精确字节数 | 来源信息 | 索引状态 |
|---|---:|---|---|
| `record.tar.gz` | 50,394,926,225 | AID `G2A0004C202013`；软件2.1.x；task 4为主 | 已完成 |
| `record_20260409.zip` | 193,356,130,953 | 软件2.2.0；task 4 | 已完成 |
| `record_20260413.tar.gz` | 215,996,415,546 | 新AID；软件2.2.0；task 5 | 已完成 |

合计459,747,472,724 bytes，约459.75GB十进制。三个归档不是预定义的
train/validation/test。准入通过后统一做episode级分层划分。

## 3. 已完成的归档索引

### `record_20260409.zip`

- 401 episode；
- 371条metadata有效；
- 367条同时存在`aligned_joints.h5`并通过归档级预检查；
- 34条缺H5，其中30条同时标记600秒异常；
- 两条异常视频实查只有3.8秒和16.33秒，码流可播放但采集未完成。

缺H5的episode直接排除，不从视频伪造action。

### `record.tar.gz`

服务器本地NVMe单遍流式索引结果：

- 57 episode；
- 55条归档级预检查通过；
- 2条缺H5；
- 其中1条还缺三路目标视频和三路时间戳；
- 56条来自软件2.1.1/task 4，1条来自软件2.1.0/task 0。

结果保存于`server/processed/manifests/record.index.json`。归档级预检查只证明
metadata和必需成员存在，不能替代episode H5及视频审计。

## 4. 三归档受控抽样

2026-07-27分别抽取一条完整正常episode：

| 归档 | Episode | 时长 | H5对齐帧 |
|---|---|---:|---:|
| `record.tar.gz` | `5c138904-5e66-468d-98cd-77d6ccc7c7c6` | 47秒 | 1389 |
| `record_20260409.zip` | `sorting_19700101095605` | 33秒 | 977 |
| `record_20260413.tar.gz` | `sorting_20260413154256` | 37秒 | 1083 |

三条都包含14D双臂、左右手各10D的state和absolute action，以及头部、左腕、
右腕RGB和相机时间戳。数值范围和任务流程支持把三个来源放入同一34D joint
候选集合。

`record.tar.gz`的EEF诊断字段与2.2.0归档不同，但本项目不使用EEF，因此不因
该差异拆分joint数据。最终是否准入仍由每条episode完整审计决定。

## 5. 时间戳与视频证据

真实episode `sorting_19700101095605`：

- H5有977个对齐帧；
- 头部视频1004帧，H5对应原始帧1～977；
- 左右腕视频各1007帧，H5对应原始帧4～980；
- 头部对齐后963个唯一画面，左腕858、右腕819。

因此H5顶层帧号不能直接作为视频帧号。转换工具必须读取各相机`.txt`，使用
H5中的`timestamp/camera/*`精确查找；缺时间戳或无法匹配时立即排除。

三路画面已确认：

- 头部`640×400`，覆盖桌面、托盘、瓶子和双臂；
- 左右腕`1280×1056`，存在广角畸变和机械臂遮挡；
- 三路互补，未发现必须裁剪的固定黑边；
- 真实episode起止状态与固定任务方向一致。

## 6. 动态诊断

同一真实episode的action→state最佳响应滞后：

- 左右臂约2帧；
- 左手约17帧；
- 右手约19帧。

少数手指维度在单条episode中恒定或近似恒定，不能据此删除。全量审计会统计
逐维范围和唯一值，34D接口保持不变。

## 7. Episode准入规则

每条episode至少检查：

- metadata、duration、integrity和validation；
- H5及三路目标H.265、三路时间戳存在；
- 34D state/action shape和有限性；
- H5帧号与主时间戳连续；
- 三路相机时间戳可精确映射；
- H.265码流可解析；
- 任务流程完整。

不需要逐帧解码或人工观看全部视频。按归档、AID、软件版本和session分层抽查
首/中/末画面，对warning簇追加人工检查。

## 8. 全量审计与冻结结果

- 三个归档共933条原始记录，去除2个跨包重复ID和归档级不合格记录后，
  形成887条完整审计候选；
- 24进程完成887/887条H5、视频和时间戳审计；
- 880条有效，7条因时间戳等严格合同失败而排除；
- 以`dm05-g2-v1`为固定seed，按AID、软件版本和task分层；
- 最终固定为800条train和80条validation，同一episode不跨split；
- 880/880条转换完成，零失败；
- norm stats仅由800条train计算，state/action的mean、std、q01、q99均为34D。

运行证据：

- `server/processed/audits/full/audit_summary.json`
- `server/processed/manifests/full_split.json`
- `server/processed/full/conversion_summary.json`
- `server/processed/full/norm_stats/g2_sort_train_51e5eed5fcc86d2b.json`

800/80约为90.9%/9.1%，不是95/5。相比严格95/5的836/44，本次少用36条
训练episode换取更稳定且覆盖更充分的80条验证集；50K按step训练，不改变
optimizer step总数。

单位、关节名称和真实命令频率必须在powered motion前与G2 SDK逐项确认；
这不阻塞离线训练。
