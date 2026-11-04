# 2026-10-16 第一条保存后无法开始下一条：左腕USB断开

## 重插后补证

用户已确认重新插牢左腕USB。随后只读三次采样（间隔1秒），头/左/右的帧序号持续增长、帧龄均小于200ms，确认恢复真实新帧；未启动录制或动作。这只证明本次恢复，不等于线缆长期稳定或连续录制现场验收。下文“待重插”描述保留原排查时点。

## 已核实结论

用户反馈连续采集第二次START无开始音、网页保护保持。现场为v32；第一条 `f1_hil_20261016T024951Z_b2e7b6ad` 于北京时间10:49:51开始，10:50:29.371完成保存，1141组三图。两份manifest均complete、completed，无writer_error，payload drop_count=0、accepted=completed=1141；该计数不证明相机全链路零丢帧或训练就绪。写盘统计completed_hz=29.8698，保存路径 `/home/user/Bruno/data/hil_data/demonstration/f1_hil_20261016T024951Z_b2e7b6ad`。

状态先从demonstration_saving进入demonstration_ready，之后仍收到约109组观测才断流。10:50:33内核 `uvcvideo 1-3.2` 连续报-71，紧接 `USB disconnect, device number 6`。左腕驱动启动日志绑定D405序列号260322273933到1-3.2，10:50:34报 `VIDIOC_QBUF: No such device`。只读4242接口头/右帧龄约0.02秒，左帧龄持续增长到265秒以上；USB枚举中左腕已不存在。

故障后START计数从2增加到15，输入仍新鲜，但录制保持idle、观测ID固定2957；左腕pending=0，头/右队列仍有帧。第二条没有创建，因此没有开始音。不是保存线程卡死，也没有证据将本次归因TCP超时。缺三路新画面时拒绝新录制是现有保护；不能绕过它生成缺左腕的伪完整数据。

## 已做与待恢复

已保存只读状态/USB证据到私有 `artifacts/results/20261016-left-camera-next-episode/`。未重启相机、驱动或发送动作。请用户在静止、采集停止时重新插牢左腕USB并检查线缆；本轮尚未获得重插完成/真实新帧恢复证据，不能说恢复成功。设备此前协商USB2.1；不能仅凭480Mbps断言是带宽、线材或供电中的哪一个根因，后续须检查连接稳定性。

网页原主提示仅写“运行条件异常/保护保持”，没有直接显示左腕缺帧。已定向修正hil.js：活跃相机故障显示具体缺帧相机、未开始录制及恢复要求；三路反馈新鲜后显示画面恢复并要求释放后START；其他硬故障提示保持。未改布局或录制状态机。使用实际render方法验证缺左腕、恢复、无关故障三种状态与现代Node语法，HTTP哈希读回通过；未声称本轮完整浏览器验收。

静态文件SHA256 `ee85411eec8a604988f78458b02aecf4aa2120cf795ee2db29d52ee937d97ac9`，原地备份 `/home/user/Bruno/DAgger-runtime/deployment-backups/20261016-left-camera-hint`。v32控制代码与配置未变。恢复后仍需用户按原三端流程启动并验证两条连续录制；不将USB断开改为自动播放开始音或自动启动。

用户随后要求强化状态检测提示，已将具体相机无新帧/恢复状态提升到主标题，分别验证左腕、右腕、头部与无关硬故障分支。最终HTTP SHA256 `f71a6d45e7959ac8ec3137b5ed705a13f206a1beae7bff90178c0ac76f06851d`，增量备份 `DAgger-runtime/deployment-backups/20261016-camera-status-title`；其余布局及运行端保持。
