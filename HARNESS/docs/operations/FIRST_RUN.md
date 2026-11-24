# 第一次运行 F1 Harness

第一次上真机，从 [F1infra 任务页](http://192.0.2.7:4242/#tasks)进入「抓积木」。先查[当前状态](../../agent/STATUS.md)中的部署和可用路线，再按[真机运行说明](DIRECT_ROLLOUT.md)核对所选执行方式、点击开始和查看结果。

想先熟悉技能调用和结果字段，可以在 6000D 运行下面的离线示例。默认的 `baseline` 执行固定程序，模型调用数为零，机器人和观测均由模拟器提供。现场装配与粗标定档案见[维护说明](SITE_STARTUP.md)。

## 1. 可选：在6000D体验软件模拟

以下命令在 **6000D 的终端**运行，使用模拟后端。

```bash
cd /home/user/projects/HARNESS
.venv/bin/python -m f1_harness.cli doctor
.venv/bin/python -m f1_harness.cli offline
```

`offline` 检查依赖后，用已审阅的 Python 技能程序依次执行接近、抓取、抓住验证、在手检查、转移、对准、插入、释放与放置验证。示例实际使用 RPent、PyRUA 和 bwrap。

然后体验失败恢复：

```bash
.venv/bin/python -m f1_harness.cli offline --fail grasp --fail insert
```

该命令在模拟后端注入一次抓空与一次插入失败。看结果中的 `measured.outcome`、`synthetic` 与 `assisted`，以及 `turns` 中实际调用的技能。`synthetic: true` 表示模拟；`autonomous_success: true` 也只适用于这一模拟任务。

如果想直接体验模型写 Python 调技能：

```bash
.venv/bin/python -m f1_harness.cli offline --planner astra --fail insert
```

显式选择 `--planner astra` 会真实调用配置的模型并产生 API 用量，机器人后端仍是模拟器。现有 `API.MD` 只给 planner 提供模型凭据；不用打开、复制到终端或传给 worker。它不承担机器人运动授权。

## 2. 怎么看结果

每次命令会打印本次 `run` 的绝对路径，运行原件在新建的 `runs/<身份>/`。将这个路径传给 `report`：

```text
.venv/bin/python -m f1_harness.cli report <刚才输出的run绝对路径>
```

不想先运行，也可以直接查看本轮已经完成的真实 Astra 模拟记录：

```bash
.venv/bin/python -m f1_harness.cli report \
  artifacts/baseline-startup-20261104/demo-astra
```

| 内容 | 含义 |
|---|---|
| `steps` | 每个技能的结果、原因与耗时 |
| `measured` | 后端实际验证结论；不是模型自己说“完成了” |
| `assisted` | 是否发生人工帮助；不会因新授权而清掉 |
| `llm_calls`／`llm_seconds` | 模型调用次数与累计响应耗时 |
| `failure` | 进程或协议失败；需要与技能失败区分 |
| `result.json`／`failure.json` | 完成或异常摘要 |
| `trace.jsonl` | 完整事件；远程任务会导入owner原始事件并保留原event ID |

模型说成功，但没有放置验证，不能算任务成功；`pending_review` 是缺少可靠证据。模拟退出码2表示任务未验证完成，不意味着必须重装环境。`worker.stderr` 用来排查隔离执行问题，不能通过关闭隔离来绕过。

## 3. “技能”实际是什么

模型能调用的机器人能力来自白名单，例如 `robo.skill("grasp")` 和 `robo.skill("insert")`。每次调用绑定当前任务、观测、标定、owner实例、epoch和请求ID；本地owner检查后才能执行。

PyRUA让模型在一个Python程序中组合这些技能、检查结果并做有界分支。RPent负责工具调用与通信。F1原owner负责最终轨迹、保护与发布，模型不直接发送关节命令。后续将BC接成技能时，改变的是技能实现，不必重写整套上层框架。

```python
# 用于理解流程，不是现场独立发动作脚本。
result = robo.skill("grasp")
if result["outcome"] != "success":
    raise RuntimeError("停止并检查抓取结果")
verified = robo.skill("verify_grasp")
```

## 4. 首次真机运行

4242 新配置默认「Harness Agentic」。已有配置会显示上次保存的执行方式。「Harness＋VLA」需独立 VLA 服务就绪。选择与本次现场验证相符的可用路线后，由用户在任务页点击「开始任务」，原 owner 接受绑定当次任务的授权后执行。

完整操作、停止和查看实测结果统一见[真机运行说明](DIRECT_ROLLOUT.md#开始一次运行)。示例中各层的职责见[单块分拣示例](../ARCHITECTURE.md#单块分拣示例)。

## 5. 粗标定开发结果怎么理解

用户已经接受粗标定用于开发运行，单独 shadow 不再是本轮前置。档案明确保留 `precision_certified=false`；尺寸、孔口、抓取偏移与夹爪参数分别标记为实测、估计或配置。设置的插入进给量不表示已测得孔口有效深度；环境碰撞余量与相对持物阈值也不是绝对精度证书。

首轮通过完整日志定位问题，再修参数或实现。固定桶遮挡延续仍需要登记的参照与当前证据；缺少证据不能凭模型描述报成功。所有尝试与人工帮助保留，首批5条用于暴露问题，后续独立20场景才用于正式成功率验收。

## 6. 真实任务结束后封存

通过任务页查看并保存本次结果，具体停止与收集操作以[当前页面说明](DIRECT_ROLLOUT.md)为准。封存必须关联同一任务的 planner 与 owner 原件，不能拿旧 BC 录制补证据。

<details>
<summary>维护附录：手动封存同一任务的原始记录</summary>

现场 owner 正常结束后，在6000D取回对应原件，维护人员可使用以下参数形式：

```text
PYTHONPATH=../DAgger/F1/src:../DAgger/shared/src .venv/bin/python -m f1_harness.cli \
  seal <本次planner-run> --owner-run <取回的owner-run> \
  --output <新的封存目录> --render-video
```

</details>

封存复用原始录制审计和既有视频生成器，检查任务、owner、epoch、发布、反馈及图像哈希。原始记录保留；缺证据返回待审，人工帮助单列，`training_ready=false`。`report <封存目录>` 会再次核对文件哈希；正式20场景评估只把证据完整、绑定冻结场景的封存结果计为自主成功。
