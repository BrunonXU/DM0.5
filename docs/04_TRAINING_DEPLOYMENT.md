# DM0.5 × G2 训练与部署运行合同

本文只冻结配置关系和验收顺序，不记录服务器专属地址或密钥。

## 本地 4090D

职责：

- 三路视频/H5/JSONL loader 验证；
- 单 batch 34D forward/backward；
- 小样本过拟合和最多约10～100 step LoRA smoke；
- checkpoint 保存/恢复和单卡 HTTP inference；
- shadow client 延迟与输出形状验证。

本地 smoke 不用于判断最终任务能力。DM0.5 base 只在进入实际模型 smoke 时
下载，Docker构建和数据转换均不依赖权重。

## 服务器 4卡与8卡

第一轮统一使用：

- action dimension：34；
- action representation：absolute joint position；
- chunk size：50；
- global batch：32；
- 三路图像：head、left wrist、right wrist；
- LoRA主体 + 完整可训练 action projections/time modules。

候选配置：

| GPU数 | per-device batch | gradient accumulation | global batch |
|---:|---:|---:|---:|
| 8 | 4 | 1 | 32 |
| 4 | 4 | 2 | 32 |

如果单卡 batch 4 OOM，则成比例降低 per-device batch、提高 accumulation，
但保持 global batch 32。先对同一数据子集各跑500～1000 step，记录：

- step/s 和 samples/s；
- 单卡峰值显存；
- GPU利用率；
- dataloader等待；
- loss有限性和梯度有限性；
- checkpoint保存/恢复时间。

根据实测吞吐和任务期限选择4卡或8卡，不把8卡作为硬要求。

## 正式训练

- 初始目标50k step；
- 每5k step保存候选 checkpoint；
- 100k是上限，不是默认必须跑满；
- validation只能按完整episode读取；
- norm stats只由train split计算；
- 只有离线趋势和早期真机结果仍改善时才继续到更高步数。

每次运行必须归档：

- OpenDM commit；
- 工作树diff或项目commit；
- Docker image ID；
- 数据split manifest及其hash；
- 34D action contract；
- prompt；
- 完整CLI参数；
- world size、global batch和GPU型号；
- norm stats和checkpoint清单。

## 远程推理

服务器输入顺序固定为：

1. head RGB；
2. left wrist RGB；
3. right wrist RGB；
4. 当前34D measured state；
5. 固定英文prompt。

服务输出必须是 `chunk_size × 34`。第一阶段客户端只运行shadow mode。接入
powered motion后，机器人本地控制器而非模型服务负责：

- joint index映射；
- 单位转换；
- 插值与速度/位置限幅；
- watchdog；
- 超时safe hold/stop；
- 只执行chunk前缀并滚动重规划。

当前项目的 `g2_sort/deployment/shadow_inference_client.py` 故意不包含G2
执行器调用，避免在SDK合同未核对时把网络模型输出直接下发真机。
