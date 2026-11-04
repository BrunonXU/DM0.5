# F1离线提示语音：豆包生成与发布

豆包只在6000D制作提示语音时调用；机器人运行时播放打包的WAV，不联网合成、不等待音频完成才处理接管。现有六种正常提示音保留，替换范围仅为异常中文语音。

2026-11-04：四条短语音已按用户授权随V47正式部署，保留V46回退。豆包低沉女声已重新生成：**请评分、连接中断、动作受限、系统故障**。正常六种音色不变，详细原因与恢复方法在终端显示。

本轮控制发布V47，沿用V46评分协议及Windows操控包；实时发布状态以[任务STATUS](../../../tasks/F1_ComplexyToys/STATUS.md)为准。[Why&How](http://192.0.2.1:4243/?html=f1-rlt-controls)同步四条短语音，旧六句试听属于被替代候选。

## 开通与本地配置

1. 登录[火山引擎豆包语音控制台](https://console.volcengine.com/speech/new/setting/apikeys)，开通豆包语音合成2.0，按账户要求确认服务与计费，再创建语音API Key。普通豆包聊天登录或方舟文本模型密钥不等于语音服务权限。
2. 在6000D的Linux终端执行以下命令，将密钥写入用户私有文件；输入隐藏，不放进聊天、源码、命令历史或机器人包。

API=密钥已配置到本机私有文件，权限600；不在文档展示。

```bash
install -d -m 700 "$HOME/.config/f1"
python3 -c 'import getpass,os,pathlib; p=pathlib.Path.home()/".config/f1/doubao_speech.key"; key=getpass.getpass("Doubao Speech API Key: ").strip(); assert key,"Empty key"; fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600); os.fchmod(fd,0o600); os.write(fd,key.encode()); os.close(fd); print("Key saved locally")'
```

3. 生成到新的预览目录，不能覆盖现有包。默认使用[官方音色表](https://docs.volcengine.com/docs/doubao-voice/tone-list-3?lang=zh)中的魅力苏菲2.0 `zh_female_sophie_uranus_bigtts`，通过官方`additions.post_process.pitch=-2`轻微降低音调；账户需具有对应音色权限。

```bash
cd /home/user/projects
python3 DAgger/F1/scripts/76_build_doubao_voice_pack.py \
  --api-key-file "$HOME/.config/f1/doubao_speech.key" \
  --output /home/user/Artifacts/F1/voice-doubao-preview-V2
```

生成成功后先通过Why&How试听，再随完整控制发布包替换异常WAV；安装不等于现场已出声。若生成失败，旧音频保持，报错不输出密钥或请求头。

## 技术合同

采用[官方HTTP单向流式接口](https://docs.volcengine.com/docs/doubao-voice/unidirectional-streaming-text-to-speech-http?lang=zh)，`seed-tts-2.0`，16kHz单声道16位PCM；收到明确完成码且音频完整后写WAV。全部短句生成成功才发布新目录，并记录文本、音色、时长和SHA256。

显式评分语音为“请评分”，终端继续说明↑成功／↓失败。未评分仅拦正常交回VLA与Y；START接管、急停和异常封存不被语音或评分阻塞。异常声音失败时以终端状态为准。播放队列收到音频不代表扬声器实际响起，现场听验单列。

## 四条短提示与验收

| 事件 | 文案 | 含义 |
|---|---|---|
| confirm_rlt | 请评分 | 未评分交还／Y，START接管不受阻 |
| communication_wait | 连接中断 | 真实断连；短时输入抖动、RTC等待不播报 |
| motion_limited | 动作受限 | 人工／策略候选碰撞边界拒绝 |
| system_fault | 系统故障 | 录制失败或输出／反馈／策略故障；具体恢复看终端 |

同一次异常去重，恢复取消过期提示；RB恢复确认只显示文字。四条成品位于`/home/user/Artifacts/F1/voice-doubao-short-V2`。V1六句试听原件保留，不进入新运行映射。音频、反馈与构建器38项本地测试通过；部署及ARM证据见任务发布记录。
