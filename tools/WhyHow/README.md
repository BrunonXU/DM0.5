# Why&How：交互讲解与文字阅读

这里把可以操作的HTML讲解和适合连续阅读的Markdown放在同一个入口。固定在6000D的4243端口，默认进入F1任务首页：先看当前进度与操作，再看配方与结论，原理和历史按需打开。

开发源已于2026-10-23移到`~/Tools/WhyHow`，网页程序仍部署到用户指定的`Artifacts/Why&How`。教学文档仍从所属任务的原件读取，刷新即可看到最新内容，避免维护两份不同的结论。这里只提供阅读，不启动训练或机器人动作。

```mermaid
flowchart LR
    A[固定4243入口] --> B[交互HTML]
    A --> C[Markdown阅读]
    C --> D[所属任务原件 / 刷新更新]
```

## 打开

- 6000D局域网：[Why&How](http://192.0.2.1:4243/)。
- 已连接同一Tailscale网络：[Why&How](http://192.0.2.29:4243/)。
- 日常入口：[F1任务首页](http://192.0.2.1:4243/?doc=f1-home)、[当前进度](http://192.0.2.1:4243/?doc=f1-status)、[现场命令](http://192.0.2.1:4243/?doc=f1-startup)。
- HARNESS：[实施方案与验收门槛](http://192.0.2.1:4243/?doc=harness-implementation)、[上游复用与适配边界](http://192.0.2.1:4243/?doc=harness-upstream-reuse)。当前介绍分阶段方案，不代表控制、模型调用或真机闭环已经实现。
- [逐帧窗口演示](http://192.0.2.1:4243/?html=action-windows)。
- [F1倍速与RTC动画](http://192.0.2.1:4243/?html=rtc-speed)：比较同路径倍速、分阶段速度、RTC截止预算和任务用时；教学设计，未部署机器人倍速。
- [F1当前推理框架](http://192.0.2.1:4243/?doc=f1-inference)：`F1_INFERENCE_CURRENT`主文档，集中RTC参数、延迟改进与平滑细节。
- [V1_FullSFT已执行配方](http://192.0.2.1:4243/?doc=v1-fullsft)、[Full 1K复盘](http://192.0.2.1:4243/?doc=full-1k-review)。
- [F1-infra 全项目关系图](http://192.0.2.1:4243/?html=graphify-f1-infra)与[Graphify 试用复盘](http://192.0.2.1:4243/?doc=graphify-f1-infra-review)：2026-10-23开发快照，核对检索收益与配置/HTTP覆盖边界。

固定的是端口4243；局域网地址是部署时实际地址，未在本项目修改服务器网络配置。**4242页脚的`Why&How ↗`已按确认方案上线**，点击在新标签页打开本站；发布只更新静态入口，后端没有重启。入口后续修改仍遵循F1-4242的页面确认与发布规则。

## 运行与维护

| 内容 | 位置 |
|---|---|
| 开发源 | `/home/user/Tools/WhyHow` |
| 部署目录 | `/home/user/Artifacts/Why&How` |
| 用户服务 | `why-how.service` |
| 目录与分类 | `catalog.json` |
| Markdown正文 | 各条目的`source`指向任务原件 |
| 日志 | `journalctl --user -u why-how.service` |

在6000D本人账号中查看或重启：

```bash
systemctl --user status why-how.service
journalctl --user -u why-how.service -n 60 --no-pager
systemctl --user restart why-how.service
curl --fail http://127.0.0.1:4243/api/health
```

服务由systemd用户服务托管，不依赖VS Code终端。本次已核验服务`active/enabled`与账号`Linger=yes`，已配置持续运行；没有为验收而重启服务器。部署回执见`artifacts/deployment.json`。

开发预览只监听本机：

```bash
cd /home/user/Tools/WhyHow
/usr/bin/python3 server.py --host 127.0.0.1 --port 18443
```

## 阅读与统一风格

2026-10-20按用户确认的预览统一整站：Markdown正文、交互讲解、目录抽屉、工具栏、图片弹窗和加载/错误提示均使用近白底、深色无衬线字体及少量陶土色强调。字体可选20/22/24px；`Ctrl/Cmd + K`打开并搜索目录，`Esc`关闭目录。文章中的表格、代码复制、原图和Mermaid仍可使用。

交互HTML在响应时附加`web/interactive.css`，嵌入与独立窗口都使用同一主题；源教学HTML的数据、脚本和原件不被改写。新交互页面需复用该主题，并保留有意义的数据状态颜色。站点样式修改不等于改写训练结论。

## 添加教学内容

在`catalog.json`增加一个条目，使用稳定的`id`、类型`markdown`或`html`、标题、简介、分类和原件绝对路径。网页点击「刷新」会重新读取目录和正文，不需要为正文修改重启进程。

目录按“01先看这里→02配方与结论→03按需学原理→04历史复盘”排序；保留既有id，避免破坏旧链接。只登记人类需要的入口和汇总结论，不逐个登记测试回执。仅改目录时先备份并定向同步`catalog.json`，无需重启阅读站。

当前文档读取范围为F1积木任务、`HARNESS/docs`与`WhyHow/content`。HARNESS只开放人类说明目录，项目根、`API.MD`、`agent/`与运行产物不在读取范围。添加其他任务时，应显式扩展`server.py`的`DOCUMENT_ROOTS`，不开放整个HOME；路径穿越和逃逸符号链接会被拒绝。文章中的其他任务Markdown、图片和附件需在该范围内才能点击。网页没有写文件或控制机器人API。

Markdown先在服务端转换并过滤危险标签，再在浏览器渲染。交互HTML在隔离iframe中运行；独立窗口用于更宽的演示。程序不公开目录列表，模型、原始录制和凭据不作为静态目录提供。

## 发布与检查

开发完成后运行定向发布脚本，仅复制本项目程序到指定部署目录，不扫描或覆盖其他Artifacts。已有部署会先备份；新增文件、用户服务和监听端口的操作遵循执行环境权限。

```bash
cd /home/user/Tools/WhyHow
/usr/bin/python3 -m unittest discover -s tests -p 'test_*.py'
/usr/bin/node --check web/app.js
/usr/bin/python3 deploy.py
```

以下验收产物均位于开发源`/home/user/Tools/WhyHow/artifacts`。2026-10-20整站阅读改版已完成浏览器验收；摘要见`redesign-acceptance.json`。后端8项检查通过，三个桌面尺寸、全部6篇Markdown、真实表格/流程图/图片、章节定位、复制链接与HTML滑条均已核对。

- `browser-redesign/report.json`：全量检查，保留发现目录搜索框Esc行为问题的真实失败记录；之前已通过的正文与图像检查不因该问题作废。
- `browser-redesign-controls/report.json`：修复后的目录、Esc焦点返回、快捷键、选文关闭、字号切换、HTML/Markdown后退前进复测通过。
- `browser-redesign-production`：发布后线上检查与实际截图的归档位置。
- `deployment.json`：部署文件哈希、服务身份和4243健康检查。

原始站点的`browser-acceptance.json`、`browser-production`和`browser-routes`保留为历史证据。桌面交互内容较宽时可以使用独立窗口。设计预览保存在`20261020-reading-redesign`，实际上线截图以改版验收目录为准。

运行依赖为本机Python 3与Python-Markdown（本次已有3.3.6），前端不依赖在线CDN。Mermaid固定10.9.3，文件来自jsDelivr对应固定版本，许可证随`web/vendor/MERMAID-LICENSE`保存；相关官方说明：[Markdown扩展](https://python-markdown.github.io/extensions/)、[Mermaid使用](https://mermaid.js.org/config/usage.html)。
