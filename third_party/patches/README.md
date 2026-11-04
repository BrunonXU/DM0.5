# 上游子模块本地改动

本目录保存本次源码快照中 OpenPI 与 RLinf 相对于固定上游提交的完整本地差异。子模块远端保持原上游，本地开发内容以补丁单独保存；LeRobot 没有本地修改。

2026-11-04 已在对应干净基线验证两个补丁可应用，应用结果与本机源码一致。恢复新克隆时按下列命令应用一次；已包含这些改动的开发目录无需重复应用。

| 依赖 | 固定提交 | 本地差异 |
|---|---|---|
| OpenPI | `15a9616a00943ada6c20a0f158e3adb39df2ccac` | modeling_siglip.py 开头空行 |
| RLinf | `d00fe72060a5aa1554e92fe7bf72846d43d48c1c` | F1 所用 OpenPI/RLinf 模型与缓存适配，8 个源码文件 |

```bash
git submodule update --init PI0.5 RLinf third_party/lerobot
git -C PI0.5 apply --check ../third_party/patches/openpi-local.patch
git -C PI0.5 apply ../third_party/patches/openpi-local.patch
git -C RLinf apply --check ../third_party/patches/rlinf-local.patch
git -C RLinf apply ../third_party/patches/rlinf-local.patch
```

其他独立检出的来源与提交见 [上游清单](../../workspace/upstreams.json)。本机多工作树布局与 GitHub 合并布局的差异见 [源码快照说明](../../workspace/SOURCE_SNAPSHOT.md)。
