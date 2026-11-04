# F1 RTC 交互设计演示

2026-10-19：按用户要求制作的本地HTML，用官方RTC Figure 3、Eq.5和Algorithm 1解释动作段衔接，并展示F1候选参数与已有延迟证据。这是设计模拟，不连接机器人，不修改训练、推理服务或部署合同。

推荐保留F1当前RLinf `openpi_rlinf` 后端，另行适配官方完整VJP引导。默认H50、30Hz、s_min20、初始延迟预算20步，对应20步强引导、10步软约束、20步新预测；最终预算必须根据完整RTC实测更新。页面同时解释训练时RTC，尚未实施相关训练。

```mermaid
flowchart LR
  A[选择F1或论文预设] --> B[查看前缀与软掩码]
  B --> C[播放异步时间轴]
  C --> D[检查覆盖与延迟预算]
  D --> E[比较实现与训练方案]
```

## 查看

- 自包含页面：[dist/index.html](dist/index.html)，无外部脚本、字体或网络数据依赖，可直接用浏览器打开。
- 6000D预览：`http://127.0.0.1:8765/`，只提供`dist/`目录。
- 已通过当前VS Code Remote的`code --openExternal`发起本地浏览器打开。远端loopback地址不自动等于笔记本地址；若当前窗口没有转发，在VS Code的“端口”面板转发8765。
- 手动方式：在**笔记本终端，任意目录**执行下面命令，然后浏览器访问`http://127.0.0.1:8765/`。使用现有6000D账号认证；若本地端口被占，可将`-L`后的第一个8765换成其他空闲本地端口。

```bash
ssh -N -o ExitOnForwardFailure=yes -L 8765:127.0.0.1:8765 user@192.0.2.1
```

服务使用6000D的独立tmux会话`f1-rtc-preview-8765`。控制台日志为本目录`preview.log`。停止仅本页面服务：在**6000D Bash，任意目录**执行`tmux kill-session -t f1-rtc-preview-8765`。

需要重新启动时，在**6000D Bash，Projects根目录**执行：

```bash
tmux new-session -d -s f1-rtc-preview-8765 'exec python3 -u -m http.server 8765 --bind 127.0.0.1 --directory /home/user/projects/tasks/F1_ComplexyToys/inference/visualizations/rtc-explainer/dist >> /home/user/projects/tasks/F1_ComplexyToys/inference/visualizations/rtc-explainer/preview.log 2>&1'
```

## 交互与参数语义

四个预设为F1建议、论文真机、优化目标、延迟超标。论文真机使用50Hz、s_min25；F1用30Hz、s_min20。两者有意区分，不能将频率滑块当作修改实际模型合同的操作。模型H实际固定50，其他H只作窗口关系示意。

图中`d̂`是预计延迟，用于强引导前缀；实际到达由`ceil(延迟/周期)`计算。规划图用`s=max(s_min,d̂)`说明稳态预算，官方实际调度的s取决于上一次真实耗时。官方理想离散模型的floor与本页工程预算采用的ceil、额外1步余量已明确区分。

有效区间为`d̂≤s≤H−d̂`。不满足时不生成有效软掩码图；延迟超预算、旧预测耗尽分别显示异常。W=1表示最大引导，不代表模型输出逐位等于输入前缀。Control拥有已提交关节动作。

K表示相对新chunk起点的可执行验证覆盖终点。在固定稳态预算下至少覆盖到`s+d̂`，可另留2步候选余量；这不是对所有前缀重新求IK的要求。已提交计划保留，新增后缀才求IK；软引导用的完整预测尾段不等于可执行轨迹。更长IK覆盖的耗时与拒绝率尚未测量。

下载按钮导出JSON设计草案，标明不可直接部署；页面中的配置字段不是现有RLinf解析器支持的正式YAML。初始20步预算依据现有同步日志，尚未包含实测RTC引导和更长IK覆盖成本。

## 证据和验证

本地数据源为`../../../artifacts/20261019-checkpoint-diagnosis/`中的`client-timing.json`、`service_log_summary.json`；实际文件从任务根定位。14000步模型共110个服务请求，最新客户端一场为采集中快照，没有客户端P99汇总。页面未将网络往返当作纯传输延迟。

2026-10-19完成：JavaScript语法检查；页面HTTP 200；Chrome直接加载同一HTML后的4个预设、播放/暂停/重置、无效参数提示、1440px桌面与390px手机布局检查，零JavaScript运行异常。浏览器HTTP导航检查遇到阻塞，因此浏览器交互验收使用本地文件；HTTP返回另行验证，不把它说成用户笔记本浏览器已验收。

参考来源：[官方RTC论文](https://arxiv.org/html/2506.07339v2)、[官方Kinetix参考实现](https://github.com/Physical-Intelligence/real-time-chunking-kinetix)、[训练时RTC论文](https://arxiv.org/html/2512.05964v2)。
