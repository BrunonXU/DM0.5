# 上游子模块本地改动

工作区保留 OpenPI、RLinf 和 LeRobot 的上游子模块。

`openpi-local.patch` 保存上传时 OpenPI 工作区相对于
`15a9616a00943ada6c20a0f158e3adb39df2ccac` 的完整本地差异
（目前仅为 `modeling_siglip.py` 文件开头增加一个空行）。

在全新克隆且已初始化子模块的工作区根目录执行：

```bash
git -C PI0.5 apply --check ../third_party/patches/openpi-local.patch
git -C PI0.5 apply ../third_party/patches/openpi-local.patch
```

当前 6000D 工作区已经包含此改动，无需重复应用。
