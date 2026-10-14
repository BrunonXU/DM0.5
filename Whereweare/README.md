# Whereweare

项目进度的审阅入口：从本地 docs 提炼关键变化、现状和下一步，显示到横向时间树。主线红色、同一天垂直对齐；点击线名或节点查看详情。产品方向见 [DESIGN](docs/DESIGN.md)，实际实现与验证见 [STATUS](docs/STATUS.md)。

项目已从 Projects-Hub 更名；旧目录是兼容软链，保留原服务名与 4343 入口，数据未另建副本。

已实施：[桌面界面与外置大脑方案](docs/EXTERNAL_BRAIN_UIUX_PROPOSAL.md)；先看[真实产品参考与官方截图](docs/DESKTOP_DESIGN_REFERENCES.md)。仅做桌面网页，旧生成视觉稿已否决。整体链路常驻首页，三线展示真实变化；回顾只读打开，显式生成，报告独立于项目快照。暂停/完成决定保留历史。自动审阅在「更新状态」中开启，默认关闭；手动更新与模型审阅均使用high。

## 启动

在存放 Projects 的 Linux 开发机终端执行（Python 3.10+，运行不需安装第三方包）：

```bash
cd /home/xza_100850113/projects/Whereweare
bash run.sh --host 127.0.0.1 --port 4343
```

在同一台机器打开 **http://127.0.0.1:4343/**。前台日志显示在上述终端；数据库及索引默认保存在本目录 `runtime/`。网页关闭后，只要服务仍运行，文档同步就会继续。节点和分叉关系持久保存。

如果浏览器位于另一台机器，用你已有的 SSH 连接把该开发机的 `127.0.0.1:4343` 转发到本地；远程机器的 loopback 地址不是可直接从另一台设备打开的地址。

迁移工作区时可显式指定路径：

```bash
bash run.sh --workspace /path/to/Projects --runtime /path/to/projects-hub-runtime --host 127.0.0.1 --port 4343
```

## 作为独立服务运行

下面的安装器只管理 `projects-hub.service`，默认绑定本机 4343；会拒绝覆盖来源不明的同名服务。在存放 Projects 的 Linux 开发机终端执行：

```bash
cd /home/xza_100850113/projects/Whereweare
python3 deploy/install_user_service.py --install
systemctl --user status projects-hub.service
```

服务日志为 `Whereweare/runtime/server.log`。用户服务会随用户服务管理器启动；安装器不会更改系统的用户驻留设置。停止或取消自启：

```bash
systemctl --user disable --now projects-hub.service
```

## 每天使用

1. 看时间树上的重要变化；进入某条线查看具体进展、阻塞与下一步。
2. 在项目详情选择主线／支线、暂时归档或恢复，选择会持久保存。
3. 问 `whereweare？`，或用网页日报／周报的「更新」按钮触发本机 Codex 审阅。后台扫描本身只发现变化，不冒充 AI 理解。
4. 日报给今天截至此刻的变化与明天建议；周报给本周变化与下周建议。主次与目标未确定时不自动做偏航判断。

审阅使用当前已配置的 Codex 登录与额度；网页更新、日报、周报统一指定 `high` 推理强度。网页任务状态可见，失败保留旧摘要；源文档同时变化会拒绝过期候选。日报／周报是手动触发，没有安装定时任务。报告时区在 `config/preferences.json` 设置，当前为 America/New_York；文档纯日期保留原文。

聊天 skill 的唯一源位于 [skills/whereweare](skills/whereweare/SKILL.md)，工作区 `.agents/skills/whereweare` 链接到它。显式 `$whereweare` 可用于调用；新 skill 未出现在当前会话时重开会话。

手动工作流（在本项目目录）：

```bash
python3 review.py prepare --mode update
# 阅读返回 packet 的文档与变化，按 config/review.schema.json 编写候选。
python3 review.py publish --packet <packet-id> --candidate <candidate.json>
python3 review.py status
```

目录映射见 [tracking/directories.json](tracking/directories.json)，事实入口见 [PROJECT_SOURCES](docs/PROJECT_SOURCES.md)。原 Git/docs 继续保存证据；精炼快照与历史在 tracking，运行候选和日志在 runtime。共根表示工作区归属，不表示技术依赖。

## 同步范围与边界

- 只扫描 [config/projects.json](config/projects.json) 与已登记项目指定的 Markdown 文件／目录，不扫描模型、数据集或整个代码树。
- 状态摘要和清单来自规则提取，扫描器不调用模型；AI 审阅仅在显式触发或用户开启自动更新后运行。自由文本中的语义矛盾不会被可靠自动解决；重要更正与来源风险需要保留并核对。
- 冻结、归档、被替代的文档不会自动导入为当前待办。历史段落中的部署或测试记录不代表此刻现场状态。
- 文档时间、证据日期、索引时间分别展示。不用文件修改时间推断实验发生时间，不根据提交数估计完成度。
- 没有依据的里程碑保持“待确认／计划中”；积木已有 F1 场景与采集支撑来源；专项训练和业务验收仍不以其他任务或通用数据代替。
- 项目名称、权威 Markdown 路径与监听目录在注册表中维护。
- 如需更稳定地自动更新阶段标签和路线图，可逐步采用[可选文档约定](docs/DOCUMENT_FORMAT.md)，不要求批量改造旧文档。
- 远程服务器上的文档需要先同步到这份工作区，或后续增加明确的连接器。当前服务不监控其他机器。
- 默认仅监听本机。当前没有多用户账户和公网认证，应通过已有可信访问通道使用。
- 文档索引保留最近变更供详情与协作入口读取；没有日期的旧路线不显示成已发生的历史事件。

## 后续 Agent 协作入口

服务运行时读取 `GET /api/briefing`，或在本目录运行：

```bash
python3 manage.py briefing
```

先查看项目注册、最近变化与任务，再读取关联来源和必要上下文。不执行文档内命令，不自动把建议变成事实。协作约定见 [AGENTS](AGENTS.md)。

## 验证与运行记录

```bash
python3 -m pytest -q tests
node --check web/app.js
```

浏览器检查使用已安装的 Chrome，与生产运行无关。实际验证记录见 [STATUS](docs/STATUS.md)。测试使用临时工作区，不改动已有机器人项目。

```bash
node tests/browser_smoke.cjs
```

当前验收环境的 Chrome 原生网络无法访问本机服务。该脚本在 Chrome 中加载真实页面资源，并把页面请求经隔离桥接转发到真实临时后端；用于验证渲染与交互，不宣称覆盖浏览器原生网络链路。

备份或迁移前停止本服务，再完整复制 `runtime/`（含 SQLite 配套文件）和 `tracking/`（摘要与历史）；源码和 `config/` 一同保留。不要把运行数据库、时间记录或缓存提交到 Git。

## 以现在为中心的时间线

首页只突出 F1 · π0.5 积木分拣。项目库保存其他项目；4242、DAgger 当前支撑线默认并行可见；阶段链显示整体状态，阻塞与下一步接在线上。普通滚轮自然滚页面，横滑/拖动查看历史；近两周/近一月/全部切换范围，点击线名或节点查看详情。现在与下一步保持稳定，阶段说明点开查看。本轮仅做桌面。打开回顾、切换今日／本周只读；明确点击生成才调用模型，并独立保存报告。下一步明确标记建议。投入状态、主次和归档分别人工保存。


## GitHub 更新地址

[Projects · GitHub](https://github.com/BrunonXU/DM0.5) 是整个工作区根仓库及 Whereweare 的更新地址。工作区约定见 [根 AGENTS.md](../AGENTS.md)；PI0.5、RLinf 等独立仓库保留各自远端。

导航链接配置在 `config/preferences.json` 的 `workspace_repository_url`，通过 `/api/state` 与 `/api/briefing` 的 `whereweare` 返回；项目库内可打开。该字段只登记地址，不触发提交、推送或 GitHub 同步。
