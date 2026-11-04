# 历史来源与未附带的证据

这些资料属于独立前的 F1 工作区，保留路径用于追溯；**不是 F1-infra 的运行依赖，也不在本源码包内**。当前部署按 [SHARING](../SHARING.md)，当前已记录状态按 [STATUS](../STATUS.md)。不得为了修复文档链接而重新打包整个 F1 工程。

| 历史资料 | 原 F1 工作区内路径 | 用途 |
|---|---|---|
| 基础能力总览 | `F1-infra.md` | 初始需求与功能汇总 |
| Hall 来源与逐文件审计 | `docs/hall_source_review_20260923.md` | 调姿交互/预设的设计依据 |
| Hall 原始归档 | `f1_hall_introduction_20260922_114704.tar.gz` | 用户提供的原始源码；SHA 见初始记录 |
| Pico 比较与采用方案 | `docs/pico_comparison_and_adoption.md` | LXL 与 meaxiao 1.4.4 的范围比较 |
| 原样 Pico 参考快照 | `reference/20260923_pico_comparison/` | dashboard、录制器等原始实现的审阅 |
| 机器人只读来源快照 | `reference/20260923_f1_09_infra/` | 接口和配置证据 |
| 机器人连接及网页证据 | `connect/robots/f1-09/evidence/infra_readonly_20260923/` | 当时的状态与供应商网页证据 |
| 通用数据流程 | `docs/dataset_workflow.md` | 原项目的数据工作流；本包录制合同见 RECORDING |
| 软件/页面/发布验证 | 原 `F1-4242/results/` | 各版本测试、截图、备份与实机只读结果 |

已采用的源码、模型及许可证留在本项目中，可直接查阅：

- [录制来源清单](../../recording/UPSTREAM.json) 与 [适配差异](../../recording/LOCAL_CHANGES.patch)。
- [Pico 来源清单](../../integrations/pico_upper/UPSTREAM.json) 与 [适配说明](../../integrations/pico_upper/docs/adaptation.md)。
- [模型清单](../../web/models/f1_wheel/manifest.json) 与 [Three.js 许可证/资源](../../web/vendor/three)。

来源清单中的原作者路径、历史 SHA、版本和许可信息应保留；不能把历史快照说成当前机器上的事实。当前打包后的实际文件由顶层 SHA256 清单核验。

## 2026-09-24 原现场脚本哈希

以下为拆分前的来源记录，不是当前独立副本的脚本哈希；使用时重新核验 `teleop/config.json`。

2026-09-24 读取的启动脚本 SHA256：

| 文件 | SHA256 |
|---|---|
| `/home/user/lxl/start_all_pico1.sh` | `4b42a5a00590ba65a19c9e53f3438fa8657d21c15b531b3e1f00ff1d72e39d1c` |
| `/home/user/lxl/stop_all_pico.sh` | `99af9e4efc1f2f6bf4f90b3d388de03bf18df0c607975313d2726aa39f4f200d` |
| `/home/user/Bruno/F1/pico_upper/current/start.sh` | `333bba9a4003d7a59d6bcbc4bb46c782de1c2c2c0476e8fddca464a6ddf31e8a` |
| `/home/user/Bruno/F1/pico_upper/current/stop.sh` | `b2cdabf381bf15956ea39891f092590b504bbe4fb73d2abaf1acdf721815283a` |

