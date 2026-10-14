---
name: whereweare
description: 当用户问“whereweare？”、“现在项目做到哪了”或要求 Whereweare 日报、周报时，增量审阅本地 Projects 文档，精炼项目变化并发布到时间树。不用于普通代码修改或单个技术问题。
---

# Whereweare

用户要的是对项目进度的掌握感：少量真实变化、阻塞和下一步，详情可追溯。项目根是此 skill 所在 `skills/whereweare/` 的上两级；兼容旧名称 Projects-Hub，但唯一实现为 Whereweare。

1. 先读项目 `AGENTS.md` 与 `prompts/review.md`。按请求选 `update`（默认）、`daily` 或 `weekly`，只读对应 `prompts/<mode>.md`。
2. 在 Whereweare 目录运行 `python3 review.py prepare --mode <mode>`。update/baseline 扫描登记文档并生成含 ID、上一快照、变化和证据指纹的 packet。daily/weekly 只读取已发布快照，按周期取 period_events，不扫描或更新项目事实。先看变化和权威入口；被截断、矛盾或结论依赖的部分用 `python3 review.py read-source --packet <id> --path <path> --start <行> --end <行>` 按需补读；候选 reviewed_sources 只确认实际读过的范围，未读不得视为已审。核对 tracking/directories.json 和顶层目录，发现新目录时只登记明确的文档入口，未归类的说明未知。不要通读全仓库代码。
3. 依 `config/review.schema.json` 生成候选 JSON 到 packet 所属 `runtime/reviews/<id>/candidate.json`。每条事实给真实 path/line；事件日期需引用明确含完整日期的原文行，并核对该日期确属这件事。复用上一版内容时移除发布器追加的 evidence.sha256。所有上限都是上限，不要凑条数。首次盘点明确历史基线，不把旧证据算作今天成果。
4. 运行 `python3 review.py publish --packet <id> --candidate <候选路径>`。源文件或上一快照已变化时，本轮候选不能直接覆盖；重新 prepare 并复核变化后再发一次，若持续变化，保留旧摘要并说明。不要绕过指纹校验。
5. 读取 `python3 review.py status` 验证发布版本。更新写入 tracking/current.json；回顾写入 tracking/reports.json，按时区/周期/事实与决定版本复用，不反向更新首页。网页会读取相应结果；简短回复最重要变化、阻塞和下一步，并说明是否已更新页面。

不要擅自更改主次线、投入状态、归档、原项目文档或确认目标。来源是事实依据，Whereweare 摘要是审阅产物，原始 Git/docs 继续保留。学习或开发建议只在能帮助当前已确认目标时给一条，并标为建议；不凭文档更新时间推断人的投入或偏离。

如果用户要求的是产品设计、只讨论或不更新，就只讨论，不运行发布流程。网页按钮调用独立本机审阅进程；不要声称这个聊天会在后台一直运行。自动选择由 skill 的描述匹配；显式 `$whereweare` 可作为调用入口。

首页按人工确认的当前主线组织，其他项目保留在项目库。读取 packet.platform / supports 与 tracking_preferences.activity：工具事件保留原 project_id，不复制成任务成果。activity_context 中的 Git/dirty/mtime 只定位待读来源，不作为成果日期。日回顾最多三条变化、一个阻塞、一个明日建议；周回顾下周建议最多三个。
