# 2026-10-15 多用户核查与 infra 镜像

当前结论与操作见 [多人使用与环境准备](../../MULTIUSER_AND_SETUP.md)。

- 核查时间：2026-10-15 11:03（北京时间）。
- 账号：从6000D通过 `h20-train`，使用 `user`，无 sudo。
- [accounts.json](accounts.json)：独立账号、组、HOME权限、登录记录、有限进程所有者/名称、软件包状态、venv引导能力和同步目标初始状态。
- 查询没有读取密码哈希、其他用户HOME内容、shell历史或完整进程命令行。
- root/opsadmin登录记录不包含真实人员身份映射，不能据此确认哪个会话属于同事。
- 初次检查时 H20 个人 Projects 下没有 infra、AGENTS.md 或 PROJECT_RULES_AND_PREFERENCES.md。

文档同步采用单向镜像、逐文件 SHA256 校验；最新验证时间和文件清单存于 H20 的 `/home/user/projects/.infra-sync-state.json`。验收后保存的独立回执用于历史追溯，不替代后续实时比较。

## 已完成的验证

- [initial-sync.json](initial-sync.json)：首次复制 infra、根 AGENTS.md 和根规则文件，17 个文件逐一通过 SHA256 校验。它记录首次版本；本回执、缓存验收结果以及后续文档补充会再同步，因此不要用首次回执判定后续文件版本。
- [job-env-check.json](job-env-check.json)：H20 当前个人账号实际 source 缓存脚本，检查 8 项目录变量、归属、写权限及临时文件读写。创建的缓存目录权限为 700，测试临时文件已自动清理；未写 shell 启动文件、安装软件或运行 GPU 任务。
- 镜像范围只含 infra 和两个协作入口；没有把其他 Projects 代码、数据、模型或凭据一起复制。
- 同步前检查远端独立改动，同步后核对完整清单和 SHA256；同步过程中应停止编辑，两端一致性保护不是并发锁。
