# 开机自启

需要日常直接打开网页时，可将工作台安装为本机用户服务。自启只恢复网页与反馈，不自动启动基础驱动、机器人动作、遥操或录制。

本步骤由部署人员完成。普通解压安装可继续使用 README 中的前台启动命令；服务安装器要求版本目录和 `current` 入口已准备好，不能直接把解压目录当成版本部署完成。

## 目录与配置

选择自己的安装根，例如 `~/F1-workbench/`，准备以下结构：

```text
F1-workbench/
  releases/<版本>/workspace/F1-infra/   源码
  current -> releases/<版本>/workspace/F1-infra/
  runtime/                            持久配置、运行记录
  systemd/                            本项目服务单元
```

部署人员将已核对的本机配置放入新源码或持久目录，在当前源码的 `configs/installation.env` 中设置实际路径：`F1_INFRA_HOME` 为上述安装根，`F1_CONSOLE_RUNTIME` 指向持久 runtime，`F1_PROFILE_PATH` / `F1_SITE_PATH` 指向本机配置；保持原共用控制锁和 4242 端口。不要复制其他机器的身份或启用标记。

`current` 指向已验证源码，`run.sh` 具有可执行权限。已有安装先按 [升级说明](SHARING.md#升级与换机) 完成备份，并结束本工作台动作、录制和遥操。

## 安装服务

在机器人对应账号的终端检查用户驻留：

```bash
loginctl show-user "$USER" -p Linger
```

需要显示 `Linger=yes`；未启用时由有权限的本机账号执行 `loginctl enable-linger "$USER"`。然后从已激活源码运行，安装根按实际位置替换：

```bash
cd ~/F1-workbench/current
HERE="$PWD"
source configs/installation.env
python3 deploy/install_service.py --home "$F1_INFRA_HOME" \
  --profile "$F1_PROFILE_PATH" --runtime "$F1_CONSOLE_RUNTIME" \
  --control-lock "$F1_CONTROL_LOCK" --port 4242
systemctl --user is-enabled f1-console-4242
systemctl --user is-active f1-console-4242
```

安装器检查机器身份、Linger、共用锁及相关空闲状态，备份旧服务单元后安装服务。遇到不同的自定义同名服务会拒绝覆盖，由部署人员核对。服务安装成功不等于整机重启恢复已验收。

## 日常维护

```bash
systemctl --user status f1-console-4242 --no-pager
journalctl --user -u f1-console-4242 -f
```

工作台空闲后，可用 `systemctl --user restart f1-console-4242` 重启服务。停止使用 `systemctl --user stop f1-console-4242`；关闭下次开机自启使用 `systemctl --user disable f1-console-4242`，后者不会结束当前进程。

服务日志和备份留在本机。只维护本工作台服务，不通过重启厂商驱动或相机解决服务安装问题。机器人断电、关机或休眠期间无法访问网页。
