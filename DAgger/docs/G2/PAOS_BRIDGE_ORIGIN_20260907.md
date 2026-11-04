# g2_paos bridge 来源与作用核查

> 历史/专项证据：本文结论对应文内日期与轮次。当前实现及下一步统一见 [当前进度](RECORDING_AND_PROGRESS.md)，按序查阅 [文档索引](00_INDEX.md)。

## 后续：用户授权停止占用服务

用户询问并要求结束该进程后，重新核对PID2481159的完整模块/bridge/只读参数，再发送SIGTERM。进程正常退出，共享`/tmp/g2_dm05_runtime.pid`已由其清理路径释放。没有使用SIGKILL、没有手动删除锁、没有启动Powered。该PhyAgentOS查询服务因此停止，后续若要恢复，必须与DAgger协调独占使用。

用户要求确认占用PID2481159由谁启动、是否为DAgger遗留。此轮只读检查，没有停止进程、没有删除PID锁、没有调用HTTP观察/推理接口，也没有读取认证令牌。

## 可确认的来源

- SSH登录账号agi，来源192.0.2.10，登录时间16:19，终端pts/2。
- 父链：sshd PID2175007 → sshd PID2175263 → 交互bash PID2175265 → bridge PID2481159；bridge启动17:21:35，锁文件17:21:36写入。
- 工作目录/home/user/xza/DM0.5，但目标进程PYTHONPATH首项是/home/user/ziyao-dev/g2-paos-integration，因此工作目录并不代表模块属于DM0.5项目。
- 实际源码目录/home/user/ziyao-dev/g2-paos-integration/g2_paos，检查了__main__.py、adapters.py、service.py、http_server.py。未发现该目录.git可供核查提交作者；源码中未取得可信作者署名。
- DAgger/40及Powered HIL模块没有启动g2_paos的代码；本地DAgger、G2Teleop、g2_robot及DM0.5检索未找到这个集成包。
- 只能确认由该SSH会话以agi运行，不能仅凭共享账号或ziyao-dev目录名认定具体人员；也不能排除用户自己的另一终端/其他代理会话。

## 功能（按源码）

命令描述为G2 query-only bridge / local PhyAgentOS relay，无Powered模式。默认桥服务监听127.0.0.1:19031，另支持本地relay；HTTP API白名单包含：

1. g2.status：报告桥服务状态/能力。
2. g2.observation：通过官方GDK读取三路相机与34D状态，写证据文件。
3. dm05.infer_shadow：调用既有DM05WebSocketBackend推理一个50x34 chunk，保存结果，executed=false/control_enabled=false。

它复用/home/user/xza/DM0.5的G2CameraRig、StrictRobotStateReader与DM05WebSocketBackend；未见Servo、send_action或move_ee_pos执行路径。__main__是HTTP serve_forever，因此不会像40的120秒运行一样自动结束；按需处理请求，并非固定频率持续请求推理。

## 为什么阻塞DAgger

--g2-read-only会创建自己的gdk.Robot和相机，因此源码明确要求--operator-confirmed-exclusive，并通过OwnerGuard占用同一/tmp/g2_dm05_runtime.pid。即使不发动作，也按其设计独占这套GDK资源。正常Ctrl-C/SIGTERM路径使用ExitStack关闭服务/相机/GDK，再释放自己拥有的锁。

结论：这是另一个PhyAgentOS/Forge只读集成服务，使用同一机器人/模型客户端；不是40启动的残留子进程。具体操作者仍须用192.0.2.10与pts/2来源确认。目录名仅为定位线索，不是人员归属证明。
