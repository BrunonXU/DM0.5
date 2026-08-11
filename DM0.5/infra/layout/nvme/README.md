# DM0.5服务器数据根目录

本目录是服务器NVMe运行布局的版本化模板。部署目标为`<NVME_ROOT>/dm05/`。

代码仓库不放在这里；代码保留在`~/projects/DM0.5`。本目录只存放无法或不应
提交Git的大文件、可再生数据产物、模型和运行证据。

目录：

- `raw_archives/`：从远程共享盘复制的三个只读原始归档；
- `staged_episodes/`：从归档单遍提取的训练必需字段；
- `processed/`：视频、JSONL、审计和manifest；
- `model_cache/`：DM0.5 base及模型下载缓存；
- `checkpoints/`：LoRA、34D密集模块和norm stats；
- `runs/`：训练配置、日志、吞吐和评估记录。

不要把本目录加入Git，不要从共享盘直接训练。
