# v1.4.2场景会话补丁：传输技术说明

日常完整操作以 [README场景采集指南](../README.md) 为准。
旧版“stop后传输仍常驻、目标在start.sh中配置”的流程已被替换。

## 当前契约

- 唯一任务配置：config/collection.yaml；scene、task、F1本地基目录、6000d基目录及私有控制入口。
- 最终目录：F1为local_base/scene/mocap_data；中央为dataset_base/scene/mocap_data。
- start冻结run/collection_session.json；录制器、预检、队列都读取同一配置，运行中改YAML不切换会话。
- stop先停本地队列再退出中央会话；最后一台退出后接收/网页服务都停止，未传数据保留。
- 配置场景时不覆盖已绑定的子数据集任务/归档去向。历史route登记支持下次跨场景补传。

## 服务与安全

F1经专用SSH密钥发送小型start/renew/stop请求，数据本身仍由6000d主动拉取。
强制命令将每把密钥绑定到真实机器人ID；仅允许固定协议，拒绝Shell、任意命令、PTY和转发。
目前控制入口为本账号独立Tailnet的192.0.2.33:22222，转到6000d已有SSH；
不是公网HTTP服务，也不修改其他用户的Tailscale实例。

SSH与Tailnet属于唤醒所需的连接基础设施，stop不关闭它们。
传输/网页用户服务取消常驻enable，由会话启动；不能在有活跃会话时直接用systemctl stop维持暂停，
因为正常心跳会恢复需要的服务。应从对应机器人退出会话。

心跳15秒，租约90秒。中央接收器每5秒检查过期会话；所有会话失联后请求停止两项服务。
这不是硬实时安全机制；网络/服务异常时stop会报告未确认并保留会话，不能假报成功。

## 多机器人与编号

中央以机器人ID＋数据集UUID＋本地episode为幂等键，同一scene加锁分配连续编号。
本地0可以映射成中央10，不能拿本地文件名推断中央位置。
不同scene独立数据集，允许同名episode出现在不同子目录。

provenance.json逐条记录：

| 字段 | 含义 |
| --- | --- |
| global_episode | 中央编号 |
| robot_id | F1_D070 / F1_D071 / F1_D072 |
| dataset_uuid、source_episode | 本地数据集代际和原编号 |
| source_root、destination | 捕获时源路径及归档场景 |
| source_files | 源路径、长度和SHA-256 |
| central_data_path、central_data_sha256 | 重映射后动作Parquet及哈希 |
| manifest_sha256、key、received_at | 清单指纹、幂等键、入库时间 |

旧数据迁移目录时保留UUID和ACK。历史source_root记录的是原捕获路径，不改写既有溯源历史。

## 完整性与原子发布

录制器提交锁和最终info.json确认保存完成后，才冻结Parquet、3路视频、episode元数据与alignment补充帧。
6000d核对SHA-256/长度、数据契约、名义时间轴、视频解码帧数与实际帧率、补充帧覆盖。
源动作/状态/时间戳保持原值，视频不重编码，仅重映射中央episode/index/task索引。

中央使用同一场景目录中的.mocap_data-transfer/revisions保存完整不可变修订，
最终mocap_data为原子替换的符号链接。数据和目录fsync后才发布、回执；
中断重试不会覆盖已有条目或重复分配编号。

训练应先readlink -f固定修订；不要手改发布后的原始数据、只备份符号链接或删除被训练引用的历史修订。
未发布修订和历史版本暂不自动清理。源数据从不因传输完成而删除，清理的是冗余传输副本。

## 验证与维护

~~~bash
# 6000d；默认检查mm，可用--root指定其他子数据集
cd /home/user/F1/PICO/pico
.venv/bin/python scripts/audit_transfer.py
PYTHONPATH=src .venv/bin/python scripts/check_lerobot_v3.py /media/user/069dd146-6f88-4f29-b859-245b1c012179/pico/mocap_data/mm/mocap_data
~~~

源码中的config/control.json继续管理运动、滤波和录制帧率等设置；
其中旧recording.output_dir/task在存在collection.yaml时被场景配置覆盖，避免双重配置。

D070专用控制私钥只保存在机器人config/private，不进入源码发布包。
D071/D072须各自安装环境、部署代码后运行6000d侧pair_collection_robot.py进行一次性配对，
不能复制D070的私钥或transfer_state。

本次生命周期及迁移的验收细节见 [场景会话验证](../reports/verification_v1.4.2_collection_sessions_2026-09-15.md)。
