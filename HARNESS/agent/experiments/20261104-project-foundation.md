# HARNESS 工程初始化与方案交付

**2026-11-04：用户指定的 `projects/HARNESS` 已形成可接续的工程基础。** 三个上游源码完成固定检出，人类方案与 agent 状态／待办分开维护，完整方案已登记到 Why&How。原 `API.MD` 保留，仅加入忽略和公开读取边界。

本轮验收范围是来源、文件与阅读入口，不包括上游运行兼容、模型请求、相机标定、控制网关或真机动作。下一阶段从最小环境和只读回放开始，不能把这次初始化记为 HARNESS 机器人闭环成功。

## 范围与实际完成

- 人类：根 README、`docs/IMPLEMENTATION_PLAN.md`、`docs/UPSTREAM_REUSE.md`；覆盖选型、现有能力复用、ROI／标定、技能协议、失败恢复、经验晋升、效率、积木例子和阶段门槛。
- Agent：稳定 AGENTS、唯一 STATUS、TODO、实验索引与本报告。
- 源码：三个独立 checkout、固定来源清单和可重复核对的 bootstrap 脚本；不安装上游或下载模型，不初始化第三方子模块。
- 配置：官方 OpenAI Responses／`codex` runtime／`gpt-6-astra`／`medium` 的示例，默认离线且无动作；未来 launcher 和认证加载器尚未实现。
- 导航：Projects README／AGENTS 增加 HARNESS 入口；保留既有其他项目的未提交修改。

## 固定来源与验证

| 源码 | 实际 HEAD | 结果 |
|---|---|---|
| RPent | `886b3b274d3dd30bbc15615ea512d65ae90bc8b3` | 官方 origin、独立 Git、clean，与锁定清单相符 |
| PyRUA-Lean | `e8f01dca744421d70684adbbc1713fd4d92f8aca` | 官方 origin、独立 Git、clean，与锁定清单相符 |
| ASPIRE | `fa00f0ef2bc1d91d594b9aa502c22133c9f538f7` | 官方 origin、独立 Git、clean，与锁定清单相符 |

机器检查见 [checks.json](../../artifacts/20261104-project-foundation/checks.json)。JSON／TOML 解析、bootstrap Python 语法、Markdown fence、本地文档链接与固定 SHA 的源码链接目标均通过。执行 `python3 HARNESS/scripts/bootstrap_upstreams.py` 再次核对三个仓库通过。

初次沙箱内 fetch 因无法联网失败，使用经自动审查允许的联网执行后拉取完成；没有改变远端或跳到其他版本。源码共约 64 MiB，是本次采样，不含新模型权重；主仓库提交锁不等于完整传递依赖锁。

## Why&How 交付

新增 [实施方案](http://192.0.2.1:4243/?doc=harness-implementation)和[上游复用](http://192.0.2.1:4243/?doc=harness-upstream-reuse)。仅在 reader 的允许范围中加入 `HARNESS/docs`；未加入 HARNESS 根目录。API.MD、agent、configs、根 README 等私密／非公开路径检查返回 404，检查不读取凭据正文。

只定向更新阅读站的 server、catalog 和维护说明，并重启 `why-how.service`；没有重启 4242、模型或控制服务。源目录与线上原有的两条 F1 目录差异被保留，原 F1 默认首页不变。

现有 9 项 reader 测试、JS 语法、公开正文 HTTP 与实际桌面 Chrome 检查通过；浏览器检查包含表格、Mermaid、目录跳转、后退和 F1 默认入口，无 JS 异常、写请求或外网请求。初版横向图渲染成功但文字偏小，随后改为纵向，最终检查见同一[发布回执](../../../../Tools/WhyHow/artifacts/harness-publication-20261104T063606Z/receipt.json)和[浏览器回执](../../../../Tools/WhyHow/artifacts/harness-publication-20261104T063606Z/browser-report.json)。

## 设计复核产生的修订

1. 增加当前控制 gate 的真实边界：机器人代理几何／CoM 不覆盖动态桶、积木与夹持物。环境／附着物检查成为首次真机前置工作。
2. 区分任务级可恢复失败与撤权／硬故障，加入 `paused` 和取消请求／owner 保持／实测停稳的不同证据。
3. 补全 base→右臂原生参考系→EEF／TCP→物体变换链；桶移动只更新其位姿，不自动否定相机标定。
4. 时间估计统一为从实施开始累计的窗口，7–12 日对应首次现场尝试；显式列出语义 mailbox、几何技能与本地纠偏的实现工作。

## 接续与未完成项

用户仍在审阅方案。没有安装核心依赖，尚未验证账户模型权限；没有创建 F1 registry／host、语义网关、动态场景检查、标定数据或经验晋升实现。下一步按 [TODO](../TODO.md) 做 fake backend、只读日志回放和模型协议 smoke；物理任务仍遵循原 owner 与现场授权。BC／RLT 保留原项目推进，不能为接 Harness 静默切换既有模型或重写结果。
