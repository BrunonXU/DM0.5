# 四项初始化

新安装运行 `bash setup.sh`，确认名字、访问 IP、数据目录和可选模块，即可生成基础工作台配置。日常使用入口见 [README](../README.md)，本页供部署人员处理自动检测未覆盖的环境。

工具只探测环境、写入新配置，不安装依赖、不启动服务或机器人。检测到环境文件不等于驱动与相机已就绪；新机动作仍需按 [部署说明](SHARING.md) 验收。已有部署升级时保留原配置，不重新初始化。

## 自动完成什么

- 获取当前账号、HOME、机器标识、架构、Python、可用 IPv4 与磁盘空间。
- 查找常见安装位置及本账号现役工作台中的 ROS / F1 / 底盘 setup 路径与 DDS 配置；不执行被探测的安装脚本。
- 复用检测到的共用控制锁。存在多个锁或不能解析的锁配置时停止写入，交由部署人员核对。
- 生成稳定机器身份；名字与网页 IP 单独存放，改名不重写动作 profile 的哈希。
- 选择 Pico 时生成本地原件录制配置；Pico / DAgger 依赖与接入仍分别按模块文档检查。

电脑连接优先使用已核验的有线IP，Wi-Fi保留为备用；这些地址与 ROS / DDS 网卡可以不同。初始化不会用网页 IP 覆盖 DDS 网卡，也不会复制另一台机器的动作启用标记。

## 生成哪些文件

| 文件 | 用途 |
| --- | --- |
| `configs/installation.env` | 当前安装的绝对路径、检测到的环境、共用锁与 4242 端口 |
| `configs/site.json` | 显示名字 `label`、首选网页 IPv4 `ip`，可选备用地址列表 `backup_ips` |
| `configs/robot.json` | 机器身份与待本机验收的只读 profile，动作默认关闭 |
| `runtime/storage.json` | 新统一录制的数据目录，默认 `~/F1-data`，归档上传关闭 |
| `runtime/recording.json` | 仅选择 Pico 时生成的统一 30Hz 本地录制配置 |
| `runtime/setup-report.json` | 检测结果与待处理项 |

初始化不创建或扫描用户指定的数据目录；真正录制时才写入数据。`robot.json` 的 `data_path` 是旧 MCAP 只读入口，不是新录制根。手柄 DAgger 的原件位置由它自身的接入配置决定，本工具不会迁移已有 DAgger 数据。

已有且内容不同的配置会阻止写入，符号链接配置也会被拒绝。相同内容可以保留，但探测结果随环境变化；本入口不作为更新配置的工具。修改名字/IP只编辑`site.json`，在工作台空闲时重启生效；不要修改`id`、`robot_id`或冻结控制profile。本站支持如下显式主备配置，`backup_ips`可省略、最多四个不重复IPv4；不允许主备同址、任意域名或控制字段。已有站点支持情况和部署状态先看[STATUS](STATUS.md)。

```json
{"schema":"f1_site_v1","label":"F1-09","ip":"192.0.2.7","backup_ips":["192.0.2.3"]}
```

这只决定网页首选展示地址和Host许可，不设置操作系统路由、DHCP或自动迁移浏览器会话；F1-09主地址目前仍是DHCP租约。手柄网页复制的SSH地址另外在`runtime/hil.json`的`ssh_host`设置为有线地址。

## 提示待处理时

| 提示 | 下一步 |
| --- | --- |
| 多个 IPv4 | 选择电脑可访问的地址；不默认选第一张网卡 |
| 未找到或找到多个环境路径 | 部署人员在 `installation.env` 核对三个 setup 文件；缺失时 `run.sh` 会停止在线启动 |
| 未找到 DDS 配置 | 核对现有驱动使用的域、RMW 和 DDS 配置；显式空配置会阻止在线启动 |
| 共用锁冲突 | 先确定各控制端实际使用的同一把锁，再运行初始化；不能另建锁绕过 |
| Pico / DAgger 待接入 | 按 [Pico](PICO.md) / [HIL](HIL.md) 准备相应运行环境与配置 |
| 动作待验收 | 按 [本机动作条件](SHARING.md#1-配置目标机器人) 核对，初始化保持动作关闭 |

只看网页可在机器人项目根运行 `bash run.sh --offline --host 0.0.0.0`，然后访问 `http://机器人IP:4242`。离线模式没有真实相机与机器人反馈；结束前台进程后再按 README 在线启动。已有 4242 服务时，不另开同端口副本。

## 自动化与隔离模拟

脚本式新安装可显式传入四项。在机器人项目根的 Bash 终端执行，路径换为自己的实际位置：

```bash
bash setup.sh --name F1-09 --ip 192.0.2.7 \
  --data-dir ~/F1-data --modules none
```

只读探测不写配置：

```bash
bash setup.sh --probe-only > /tmp/f1-inventory.json
```

导入清单可在新目录模拟生成，不冒充本机已就绪：

```bash
bash setup.sh --inventory /tmp/f1-inventory.json \
  --name F1-09 --ip 192.0.2.7 --data-dir ~/F1-data \
  --modules pico,dagger --output-dir /tmp/f1-setup-simulation
```

`--inventory` 的报告始终标记 `simulation: true`。`--output-dir` 只指定配置生成位置，不复制应用源码；完整模拟启动要先在独立目录解压源码，再在该目录初始化。保留报告中的实际发现、未验证项和动作关闭状态，不能把配置生成成功写成真实采集或动作验收。
