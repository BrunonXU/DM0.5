# 历史项目目录

这里保存从 `projects` 真正移出的 DM0.5、G2 机器人开发副本与已搁置的 Whereweare。项目文件只搬迁一份，DM0.5 与 g2_robot 保持同层；`Projects-Hub` 是本目录内指向 Whereweare 的原有别名。当前 F1 开发入口仍是 [Projects](../../README.md)。

这次整理没有提交或推送，也没有启动训练、机器人动作或 Whereweare。Whereweare 继续暂停；原有源码、SQLite 与历史时间树完整保留，恢复前需要重新核对跨目录文档来源和 tracking 存储配置。

## Git 如何保留

`Archive` 是原 Projects 仓库的 linked worktree，初始基于同一提交，处于 detached HEAD；历史与远端没有新建副本。原有未提交修改随文件搬到此工作树，在这里运行 `git status` / `git diff` 可查看。根工作区的索引对象与暂存内容保持不变，只通过 sparse checkout 排除已迁出的项目，不把整理显示成数百个删除。

- 当前提交基线：`25f3d355660b909f9851f775912dff244b758127`。
- `.git` 指向 `../projects/.git/worktrees/Archive`；这两个目录有 Git 管理依赖，不能随意删除、复制 `.git`，或运行 `git worktree remove`。
- 主工作区运行 `git sparse-checkout reapply` 会继续保留排除，不会重新展开这些项目。不要执行 `git sparse-checkout disable`，否则 Git 会把历史路径重新展开到 Projects；需调整分类时先更新稀疏规则和本文。
- 当前工作树不自动跟随 Projects 的后续提交。将来需要继续开发某个历史项目时，先检查这里的未提交修改并为其建立明确分支，再决定是否合并主线；此次没有代办这些 Git 操作。
- 根 `.gitignore` 的当前内容复制到本目录，用于继续保护已有 runtime、日志和数据；它也是未提交的工作区文件。

## 路径与恢复

| 原位置 | 当前位置 |
|---|---|
| `projects/DM0.5` | `Archive/DM0.5` |
| `projects/g2_robot` | `Archive/g2_robot` |
| `projects/Whereweare` | `Archive/Whereweare` |
| `projects/Projects-Hub` | `Archive/Projects-Hub` → `Whereweare` |

DM0.5 本机 host 启动脚本现在从自身位置推导项目根，不再依赖旧的绝对路径；只做 Bash 语法检查，没有执行训练或推理。历史报告里的旧路径是当时的证据，不批量改写。

Whereweare 的 `projects-hub.service` 保持 disabled/inactive，单元仅修正项目与日志路径，显式 workspace 仍为 Projects。原实现部分默认使用项目父目录，来源注册表也含旧路径；将来用户明确恢复时，应一起核对注册表、CLI workspace、安装器与 tracking 存储位置，不能把旧说明当成已完成恢复适配。

## 校验与证据

迁移前 3572 个目录项逐项留存清单，普通文件记录 SHA-256；同文件系统 rename 后确认所有原目录项的 inode、设备号、类型、权限、长度和 mtime 不变。迁移后原有 tracked diff 完全一致，根索引的 blob 与 stage 条目完全一致。随后仅编辑迁移导航和本机脚本路径，修改前副本另存。

完整清单、迁移回执、原有 dirty patch、索引记录和服务变更前副本在 [本次整理证据](../Artifacts/project-reorganization-20261023/archive/receipt.json)。
