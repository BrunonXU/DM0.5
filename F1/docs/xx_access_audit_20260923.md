# xx账户读取与修改范围核查（2026-09-23）

本轮只读核查权限及本F1会话的执行记录，没有改变任何账户、分组、ACL或同事目录。

## 当前访问结果

当前工具执行身份为`uid=1006(user)`，主组1008，不在xx私有组1010。工具的sandbox escalation没有将Linux身份切成root。

16:23检查xx主目录显示mode711，扩展ACL明确包含`user:user:---`。对本账号，具名用户ACL优先于group/other；无法穿越该目录。`stat /home/user/F1`、已知Pico路径及`ubuntu/ubantu/Ubuntu`路径均返回Permission denied，不能据此判断这些Ubuntu目录是否存在。

主目录ctime为2026-09-23 16:22:58.342354977+08:00，mtime为16:19:04.880128365。ctime表示inode元数据变更，不能单凭它确认变更操作或操作者；它不是创建时间，也不是本会话修改该目录的证据。

## 之前为何可读

13:46:52的实际工具输出显示：

```text
F1/                         drwxrwxr-x  (775)
F1/PICO/                    drwxrwxr-x  (775)
F1/PICO/pico/               drwxr-xr-x  (755)
F1/PICO/pico/README.md       -rw-rw-r--  (664)
F1/PICO/pico/config/ip_config.py  -rw-r--r-- (644)
```

14:27输出同时显示home711且`ls /home/user`失败，但`ls /home/user/F1`成功。这说明当时不能枚举home，却能沿已知名字访问允许的子目录。目录x允许穿越、r允许枚举；子目录/文件other位仍开放时，仅分不同Unix组不会形成完整读隔离。

没有保存当时home完整getfacl，故不声称还原了每条旧ACL；上述历史权限与成功读回是证据。当前具名拒绝ACL是新核验事实，不能倒推它在此前也以相同方式生效。

## 此前确实见过的内容

范围是`xx/F1`，不是整个xx home的完整目录清单：

| 路径 | 已知内容/覆盖 |
|---|---|
| PICO/pico | v1.4.4遥操、精调、轨迹、LeRobot录制、传输、审阅网页源码、模型与测试；本轮主要审阅来源 |
| PICO/deployment | 旧lxl部署和修复资料 |
| reset | 独立复位与模型/机器人配置、脚本及历史证据 |
| 本体平台 | lxl原版/修改版归档、Pico安装连接说明 |
| cyb_safe_control_real | 安全控制工程目录、URDF及发布产物；未全面审计 |
| openpi、ref_doc | 见过目录名，不等于完整读过内部内容 |
| dist.zip、remote_sth.zip | 见过归档文件名 |

`PICO/transfer_6000d`、`private_review_*`、部分review_data/备份目录当时已经拒绝读取。当前这些内部路径统一受父目录阻断，未进一步尝试。

## 是否改过xx的Ubuntu子目录

本F1会话及子任务可见执行记录中，未发现向`/home/user`写文件、修改权限、执行sudo或操作`ubuntu/ubantu`路径的记录。

- 参考快照写入`/home/user/projects/F1/reference/20260923_pico_comparison`。
- 上肢复制脚本明确`src=/home/user/F1/PICO/pico`、`dst=/home/user/projects/F1/pico_upper`，只写dst。110份上游来源清单中的`modified`表示自有副本与原始SHA不同，不表示修改上游。
- 实际适配与字节码缓存位于自己的F1目录；模型运行缓存位于机器人`/home/user/Bruno/F1/pico_upper/runtime`。
- 命令扫描只找到3次chmod，均给自己的console/pico脚本加执行位；未找到setfacl/chown/sudo执行记录。

这是本会话操作范围审计，不是全服务器文件完整性证明。未取得其他会话/进程的系统审计日志，不能判定其他人是否改过其Ubuntu目录。普通读取可能更新文件系统atime，不能与源码内容修改或chmod混为一谈。

证据：[当前权限](../console/results/permissions_audit_20260923/current_permissions.json)、[历史读取](../console/results/permissions_audit_20260923/prior_access_evidence.json)、[权限命令扫描](../console/results/permissions_audit_20260923/permission_command_scan.json)、[来源清单](../pico_upper/UPSTREAM.json)。

## 当前其他用户主目录的有效权限
核验时间：2026-09-23T16:29:41.937707+08:00。只查`/home`一级目录元数据和内核有效权限，没有读取其他用户的文件内容。
| 主目录 | 可列出目录 | 可穿越已知路径 | 可写入主目录 |
|---|---|---|---|
| chenyuanbo | 否 | 是 | 否 |
| cz_100902943 | 否 | 是 | 否 |
| fzf_100878174 | 否 | 是 | 否 |
| hq | 否 | 是 | 否 |
| huangpeixin | 否 | 是 | 否 |
| hwy_100878104 | 否 | 是 | 否 |
| liulu | 否 | 是 | 否 |
| nino_100904852 | 否 | 是 | 否 |
| user | 否 | 否 | 否 |
| user | 是 | 是 | 是 |
| zxl_100881968 | 否 | 是 | 否 |

除xx的具名拒绝ACL外，其余9个他人home为751，对xza仍允许穿越但不允许枚举。里面具体文件是否可读取决于沿途目录及文件的权限/ACL，不能称为整个用户空间可读，也不能称已完成全隔离。xza自己的home同样是751，其他账号是否可读其具体子路径也需单独核对。没有改动任何权限。[原始有效权限结果](../console/results/permissions_audit_20260923/current_home_access.json)。
