# Libraries：共享上游依赖

OpenPI 与 LeRobot 已从 Projects 移到这里，保留各自独立仓库、上游版本、本地补丁和官方基模。日常 F1 开发仍在 [Projects](../../README.md)；当前任务直接使用的 RLinf 留在 Projects。

这是 Projects 同一仓库历史的独立稀疏工作树，方便分别查看依赖和正在开发的项目。迁移没有新建提交、升级依赖或重下模型；历史记录中的旧路径可按下表定位。

## 当前位置

| 内容 | 位置 | 迁移前位置 |
| --- | --- | --- |
| OpenPI 仓库 | [PI0.5](https://github.com/Physical-Intelligence/openpi/blob/15a9616a00943ada6c20a0f158e3adb39df2ccac/README.md) | `projects/PI0.5` |
| 官方 π0.5 base | `PI0.5/checkpoints/pi05_base/` | `projects/PI0.5/checkpoints/pi05_base/` |
| LeRobot 仓库 | [third_party/lerobot](https://github.com/huggingface/lerobot/blob/713a409faedd73bb5597481b8885f17fbee23330/README.md) | `projects/third_party/lerobot` |
| 保留的上游补丁说明 | [third_party/patches](../../third_party/patches/README.md) | `projects/third_party/patches` |

6000D 当前 LeRobot 导出变量为 `F1_LEROBOT_ROOT=/home/user/Libraries/third_party/lerobot`；重新准备 editable 环境时使用同一路径。当前操作见 [F1 导出说明](../../DAgger/docs/F1/F1_HG_DAGGER.md#6000d审计人工审阅导出)。历史冻结包和 H20 的路径保持原意。

## Git 与保全

- 根目录使用 Projects 的 linked worktree，当前为 detached HEAD；各子模块仍保留自己的 `.git`、远端和未提交改动。需要开发提交时先为所属仓库选择明确分支，不在此处盲目执行全量子模块初始化或关闭稀疏检出。
- Projects 和 Libraries 共用 Projects 的 Git 元数据；保留 `../projects/.git` 与本目录 `.git`，不要将其中一份当作重复缓存删除。根 `.gitmodules` 保留原完整仓库定义，未显示的 RLinf 并非迁入此处。
- 两个迁出目录合计 2,618 个常规文件逐一验证 SHA256、inode、大小、修改时间及权限不变，独立仓库 HEAD、index、差异和远端逐项一致。
- 官方基模 29 个对象共 12,441,749,581 字节，逐项 SHA256 与 2026-10-15 官方来源清单一致。模型原件随目录原地重命名保留，没有复制出第二套。

[完整迁移回执](../Artifacts/project-reorganization-20261023/dependencies/receipt.json)保存逐项检查结论，同目录的迁移前后清单保存文件身份。若之后需要再搬目录，先重新核对环境、调用方和 Git 工作树设置。
