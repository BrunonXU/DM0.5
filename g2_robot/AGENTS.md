# G2 Robot 协作规则

- G2的PC是机器人内部域控，禁止执行任何系统包管理变更，包括
  `apt`、`apt-get install/update/upgrade/remove`及其`sudo`形式。缺少命令时使用
  系统现有替代工具，或在6000D完成处理，不能修改G2系统软件环境。
- 修改任何G2接口、字段、枚举或控制参数前，先查
  `docs/GDK_2.6.3_RELEVANT_API.md`，再核对其中链接的智元GDK 2.6.3官方原文。
  官方未记录的内容不得猜测；必须标记“未验证”并通过G2只读探测确认。
- 6000D与G2已接入同一公司内网。日常修改只通过`scp`传递本次变更的具体文件，
  默认不压缩、不生成交付包、不重复报告SHA，也不经过Windows中转。
- 操作者入口优先提供`scripts/*.sh`。Python负责可复用实现，不要求操作者粘贴长段
  Python或临时heredoc。
- 默认只读或Shadow。任何Servo、复位或powered动作必须显式授权、倒计时，并提示
  物理急停。不得自动从normal切换到无碰撞保护的low-latency。
- 6000D可以向G2直接`scp`代码文件，但不得通过SSH远程执行任何真机动作。所有
  GDK、Servo、复位和powered命令仍由Bruno在G2终端手动运行。
- 修改后先运行无GDK纯逻辑测试和Shell语法检查；真机未验证的行为必须明确标记。
- 每次同步G2快照后更新`docs/G2_ROBOT_STRUCTURE.md`。`docs/Bruno.md`只保留现场临时状态，
  `docs/G2_TROUBLESHOOTING.md`只作为尚未完成迁移的原始记录；跨训练/服务器/真机的权威状态、
  原理、实验和事故统一进入`../DM0.5/docs/`，并遵守`../DM0.5/docs/AGENTS.md`。
