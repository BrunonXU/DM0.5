# F1 RTC＋局部RLT V43：终端版本身份

V43已部署到局部RLT入口，终端分别显示实际控制版本与该段actor身份；Windows启动器V43包已上传，需操作者安装后才能显示本地包版本。原来的V42运行记录没有被改名，网页异常音仍是试听候选。

本次只改显示和只读状态字段，不改变动作、RTC、网络超时、限位或音效。12:42与12:44两条已证明推理超时自动重规划工作，但手柄失鲜和ACK重复重连仍打断流程；问题与下一步见[V42现场补证](20261103-rlt-rtc-V42.md#1242与1244两条v42现场补证)。不能用V43发布宣称通信故障已经修好。

## 屏幕上如何辨认

| 显示 | 身份来源 | 何时出现 |
|---|---|---|
| F1 SSH＋手柄会话 V43 | 打包时注入、安装时哈希及加载校验 | Windows启动器开始运行 |
| F1 RTC＋局部RLT V43＋清单短身份 | 运行端完成manifest校验后输出的F1 IDENTITY | 机器人入口加载完成 |
| 实际actor版本、训练step、Q更新数 | →受理时已采用并锁定的局部策略身份 | 每个新局部段一次 |

启动health可能仍显示BC400协议基准，不能代替局部实际actor。未知身份显示未确认，不根据磁盘新软链或候选号猜当前进程版本。训练step在BC与Q阶段含义不同，单列Q更新数，不把BC400误称critic400步。长期约定已加入[任务AGENTS](../../AGENTS.md#控制版本与模型身份)。

## 验证与实际部署范围

- 本地35个不同用例通过：Console12、RuntimeReporter2、Windows21。Windows回环测试初次受沙箱socket限制，单项提权复测通过；失败记录保留。
- F1 ARM14个受影响用例通过；正式SHADOW到达等待START、退出0，录制目录及last-session未变，无动作发布或新录制。
- 控制release为`/home/user/Bruno/DAgger/releases/f1-rtc-rlt-V43/DAgger`，manifest为`5e08bdcfe1643762c71f51b3193bc5bbe3483bad815f329dc9a8fdcb26d679bb`。现装配为`/home/user/Bruno/DAgger-runtime/f1-rtc-rlt-V43`，空闲持共享控制锁切换，V42备份保留。
- **只有rlt-hil实际客户端升级V43。** 普通rollout／hil仍引用各自既有的`20261030-f1-rlt-manual-compute-r3`；三模式装配检查返回的release是装配号，不能冒充三种实际控制代码都升级。
- H20组件仍V41，模型、actor更新合同及六段WAV不变；网页异常候选未部署。

Windows包在F1的`/home/user/Bruno/DAgger/updates/f1-ssh-session-V43/`，ZIP SHA256为`87ae5ac69730b1e914d8b6410c54801a8f5bee957c735e8ce5807cea5b83f646`。该包只更新三份启动／日志文件，**不包含手柄sender**。现场sender仍把部分合法恢复状态显示为未知，虽现源码已有正确提示，仍需独立核对并补齐其发布覆盖；不能把启动器升级当成sender已更新。

[安装命令](../STARTUP.md#single-window) · [源码与发布审计](../artifacts/20261103-v42-two-rollouts/version/audit.md) · [逐模式实际组件](../artifacts/20261103-v42-two-rollouts/version/deployment/actual-components.json) · [零输出回执](../artifacts/20261103-v42-two-rollouts/version/shadow-result.json)
