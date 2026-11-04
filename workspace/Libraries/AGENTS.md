# Libraries 协作入口

这里保存从 Projects 移出的 OpenPI、LeRobot 和上游补丁，目录用途与当前路径见 [README](README.md)。当前 F1 任务及 RLinf 开发仍以 [Projects](../../AGENTS.md) 的任务入口为准。

遵循 [Projects 协作习惯](../../PROJECT_RULES_AND_PREFERENCES.md)，进入子仓库时再读其 AGENTS。保留各仓库实际远端、本地修改与官方基模；涉及 F1 训练或推理时先读 [任务 AGENTS](../../tasks/F1_ComplexyToys/AGENTS.md) 和 [STATUS](../../tasks/F1_ComplexyToys/STATUS.md)。

本目录是共享 Git 历史的 linked worktree；子仓库有独立 Git 元数据。修改或提交前核对实际仓库和分支，不把根仓库地址套用于上游依赖，不自动关闭稀疏检出，不删除共享的 Projects Git 元数据。
