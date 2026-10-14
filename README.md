# 具身智能机器人研发工作区

这是一个面向具身智能研究、机器人策略训练和真机 Demo 的顶层工作区。
DM0.5 是其中一套策略栈，不是整个仓库的边界，也不是项目长期唯一的主题。

## 主要组件

| 路径 | 职责 |
| --- | --- |
| `DM0.5/` | DM0.5 训练、验证、推理服务和部署工具 |
| `PI0.5/` | Physical Intelligence 官方 OpenPI 源码，以上游子模块管理 |
| `RLinf/` | RLinf 官方源码，以上游子模块管理 |
| `g2_robot/` | G2 相机、状态、安全、控制、运维和策略运行时 |
| [DAgger](DAgger/README.md) | F1 / G2 遥操、人工接管、纠正采集与导出；独立 G2 遥操统一在 `DAgger/G2/teleop/` |
| `third_party/` | 任务复用的第三方源码，例如 LeRobot |
| `tasks/` | 任务级集成、配置、实验记录和评测 |
| [F1-infra（F1-4242）](F1-4242/README.md) | 独立机器人工作台：相机、调姿、Pico、录制与数据审阅 |
| [Whereweare](Whereweare/README.md) | 项目时间树：文档变化审阅、主次线与日报／周报 |
| [`F1/foam_sort_whole_body/`](F1/foam_sort_whole_body/README.md) | 自研 F1 泡沫棉移动操作：最终数据审查、全身字段合同与 DM0.5 任务集成 |

## 当前 Demo

第一个 PI0.5 + RLinf 任务是双臂手机装盒 Demo：

1. 从指定位置抓起仿真手机；
2. 将手机从一个夹爪交接给另一个夹爪；
3. 把手机放入手机盒底；
4. 对齐盒盖并完成合盖。

任务范围和目录说明见
[`tasks/g2_phone_packaging_pi05_rl/`](tasks/g2_phone_packaging_pi05_rl/README.md)。

## 架构边界

工作区将可复用能力与具体任务集成分开：

```text
任务定义与评测
      |       |
      |       +---- RLinf 强化学习后训练
      +------------ DM0.5 / PI0.5 策略
                         |
                      策略合同
                         |
                     g2_robot
                         |
                        G2
```

模型仓库不负责机器人安全逻辑。机器人侧代码应通过明确的观测/动作合同调用策略，
不直接依赖某个模型的训练内部实现。

## Git 内容边界

以下内容适合进入 Git：

- 源码和小型配置；
- 设计、运行和复现文档；
- 自动化测试；
- 数据、模型及实验产物的不可变清单或引用。

以下内容不进入 Git：

- 原始或转换后的数据集；
- 模型权重和 checkpoint；
- 机器人日志和训练日志；
- 生成的视频、评测输出及其他大体积产物；
- 部署压缩包、本地缓存和临时文件；
- 凭据及机器私有环境文件。

大文件应保存到对应的数据集、模型或产物存储中，仓库只记录可复现的标识与说明。

## 克隆与初始化

OpenPI 和 RLinf 保留各自的官方上游历史。克隆本工作区后执行：

```bash
git submodule update --init --recursive
```

OpenPI 自身还包含 ALOHA 和 LIBERO 子模块，递归初始化会一并获取。
`third_party/lerobot/` 同样固定到上游提交，由上述命令初始化。
6000D 上的 OpenPI 本地改动另外保存在
[`third_party/patches/`](third_party/patches/README.md)，可按说明恢复。

## 当前 PI0.5 基础模型

官方 `pi05_base` 存放在本机：

```text
PI0.5/checkpoints/pi05_base/
```

该目录被 Git 忽略，不会上传到 GitHub。可以使用下面的任务脚本进行首次下载、
断点续传或完整性复核：

```bash
./tasks/g2_phone_packaging_pi05_rl/scripts/download_pi05_base.sh
```

新任务按 `机器人/任务/models/<模型>/` 与 `机器人/任务/results/` 组织；旧任务逐步迁移。通用 OpenPI、RLinf、DM0.5 源码继续集中复用。

## 文件归位

- G2/DM0.5 控制根因、待办和交接统一从 [DM0.5 机器人文档](DM0.5/docs/robot/README.md)进入；带日期的旧交接是历史快照。
- F1 泡沫棉的结果统一放在 [任务 results](F1/foam_sort_whole_body/results/README.md)，包括 09-11 FTP 盘点和 09-14 数据审查。
- 工作区根目录只放项目、共享组件和工作区说明；临时导出包、缓存和任务产物不另占顶层目录。
- 原始数据、模型、实验日志和冻结基线按所属项目保留；历史部署包集中在 `DM0.5/artifacts/`。


### 2026-10-14 源码快照与本地资产

本仓库保存代码、文档、小型配置、测试与审阅记录；机器人STL网格、模型权重、采集数据、运行数据库、缓存与交付归档不进入新版本。文件仅从Git跟踪范围排除，本机原件保留，不重写已有提交历史。

机器人网格的路径、大小与SHA256见 [LOCAL_ASSETS.json](LOCAL_ASSETS.json)。新克隆在运行3D显示、碰撞检查或相关测试前，须从原工作站或原机器人资产来源补齐对应文件，并核验清单；保留的URDF/manifest不能替代网格本体。小型网页依赖、提示音、文档配图仍随源码保存。

PI0.5、RLinf、LeRobot沿用子模块引用，不在根仓库代替其提交或改远端；本地独立rea检出不加入根仓库。
