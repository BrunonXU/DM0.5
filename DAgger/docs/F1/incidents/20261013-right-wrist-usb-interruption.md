# 2026-10-13 右腕USB断流与示教异常收尾

## 直接证据

北京时间16:24:00，F1内核在`uvcvideo 1-2:1.1 / 1.3`连续报告`Non-zero status (-71) in video completion handler`。右腕RealSense节点350814随后持续报告`Frames didn't arrived within 5 seconds`。USB 1-2映射为Intel RealSense D405，驱动恢复时确认序列号260322276040。

同一时刻采集诊断最后回调ID为头3100、左3092、右3031，最后已消费为3038/3032/3031；右路队列为空，头/左仍有新图，但无法配出三路。输入包年龄6.8ms，写盘队列为空、writer_error=null、drop_count=0；不存在将此次归因为提示音或磁盘写满的证据。两秒相机等待到期触发`three new synchronized camera frames unavailable`，旧v21采集线程永久结束，主循环再因acquisition_error退出。

旧收尾把任何运行异常统一传成status=failed。payload/journal的close按合同不提交failed目录，返回False又被包装成两条`episode writer did not commit`。因此表面像两次磁盘失败，实际638组已持久化、无写盘错误，是运行中断与保存状态混用。

用户重新插接后，16:27:12 USB断开、16:27:16重新枚举，右腕驱动自动重建，Color/Depth均640×480@30，节点恢复Up；后续真实观测shadow成功取得三路图像。两只D405当前均协商480Mbps，尚未确认根因是线材、接头、供电、端口还是设备本身；不能将USB速度直接认定为本次故障原因，也不能声称重插已证明长时间稳定。

## 数据保留与恢复

- 前一条`f1_hil_20261013T082257Z_202d0281`已正常保存934组三图，未修改。
- 中断条`f1_hil_20261013T082338Z_46d0c0e9`原件保留在`/home/user/Bruno/data/hil_data/demonstration/`；`payload.partial`与`journal.partial`各638组对应，4701条控制、963条事件，writer_error=null/drop_count=0。
- 独立恢复副本位于`/home/user/Bruno/DAgger-runtime/recovery/f1_hil_20261013T082338Z_46d0c0e9`。先验证原始流和三路pack哈希，再以硬链接保留不可变数据，单独生成恢复manifest与来源记录。仅恢复manifest标记持久前缀结构完整、任务interrupted、requires_review=true、training_ready=false；原manifest没有修改。
- 恢复副本audit valid=true、errors=[]、observations_without_payload=0。它不在正式训练/视频自动扫描目录，不代表任务成功或已获训练批准。

## v22修复及边界

候选/正式三路线版本`20261013-f1-hil-v22-camera-interruption`仅改变F1的observation、policy、demonstration、lifecycle四个模块；G2、硬件输出、运动学、保护参数、相机配置、payload writer、音量与音频实现保持。

1. 相机等待到期使用专门CameraUnavailable异常。只重试该类缺图，不吞掉时钟倒退、反馈失鲜、文件错误等异常，不放宽两秒等待或原相机新鲜度/配对阈值。
2. 主控制线程处理缺图事件，废弃旧动作并进入原有SAFE_HOLD。无动作自动恢复；断流期间START被消费但不排队授权，三路恢复后保持，必须新的START才可继续。
3. 人工示教将中断前已接纳数据正常排空提交，manifest写termination.outcome=interrupted及原因，last-session保留中断标识，training_ready=false。中断不播放正常完成B。后续正常条不继承中断标识。
4. 采集线程仍尝试读取真实新观测，恢复事件明确requires_new_start。不重复旧图、不插帧、不把缺图期间当完整任务示教。永久USB故障需要处理硬件连接，软件等待不是硬件修复。

## 验证与发布

- 本地受影响模块30项通过；全F1/shared回归在允许loopback后257 passed/9 skipped，另补“断流先清待发运动”测试通过。首轮11项失败均为沙箱禁止socket，不是断言失败。
- F1 ARM最终48项通过，覆盖缺图持续重试、相机先恢复再主循环处理时仍保持、断流START不延期执行、正常下一条、音效与写盘故障隔离、非相机错误仍终止。
- 真实ROS观测/private19427端口shadow注入缺图：进入保持并保存中断前缀；恢复后不自动起条；新START可录下一条。两条58/55组三图均audit valid=true，测试退出0，robot_commands=0。首轮测试辅助入口误用系统Python缺MuJoCo，未进入采集；r2使用正式环境通过。未实际拔线驱动实机动作来测试恢复。
- 清单/配置SHA验证通过，确认工作台、录制、遥操空闲并持共享锁切换。正式`start_f1_hil_route.sh`已指向v22；没有远程启动powered或重启4242/电机/相机，用户音量53/50保持。
- 备份及持锁回退脚本：`/home/user/Bruno/DAgger-runtime/deployment-backups/20261013-camera-interruption/`。v21与原始数据均保留。一次部署辅助脚本语法错误发生在执行前，无远端变更；修正后完成切换并编译检查回退脚本。

全部脚本、原始测试结果与恢复审计位于项目私有`artifacts/results/20261013-camera-interruption/`。新版本的软件故障收尾已验证；实际手臂运动时线材是否仍间歇断流以及长期采集稳定性尚待现场观察。旧输入链路延迟排查仍维持暂停，此次没有调整watchdog或网络传输策略。
