# H20 用户态 FFmpeg 运行库

> 2026-10-15（北京时间）完成。目的：为 TorchCodec 0.3 / 积木数据的视频读取提供 FFmpeg 共享库，同时保留用户和项目环境隔离。

## 已完成

- H20 安装位置：`/u01/user/tools/ffmpeg-4.4.2-jammy`。
- Ubuntu Jammy FFmpeg 版本：`7:4.4.2-0ubuntu0.22.04.1`；共享库主版本包括 `libavcodec.so.58`、`libavformat.so.58`、`libavutil.so.56`。
- 共解包 **90 个 Ubuntu deb，55,082,062 bytes**。每个文件均校验包元数据中的 SHA256；使用 `dpkg-deb --extract`，没有执行 `dpkg -i`、`apt install`、维护脚本或 sudo。
- 工具前缀约 **152.2 MiB**；含下载 deb 与验收证据的 staging 约 **56.8 MiB**。均位于个人 `/u01`。
- 已验证 ffmpeg、ffprobe 及三个主要共享库的 `ldd` 无缺失项；最终路径下再验证一次。
- CPU 合成视频验收通过：**H.264、64×64、8 帧、1 秒**，编码、ffprobe 计数与解码均成功。
- 系统 `/var/lib/dpkg/status` 在解包前后 SHA256 不变。没有修改 `/usr`、`/etc`、全局动态链接器配置、训练 venv 包、任何用户 `.bashrc`。

这不是 CUDA/NVDEC 或完整训练验收。TorchCodec 与目标训练环境的联合解码验证由训练环境验收记录另行证明。

## 使用方式

在 **H20 当前训练任务的 Bash 进程**中显式加载：

```bash
source /u01/user/tools/ffmpeg-4.4.2-jammy/activate.sh
ffmpeg -version
```

激活脚本只给当前进程及其子进程设置 `PATH` / `LD_LIBRARY_PATH`，无需添加到全局或个人 `.bashrc`。使用完退出该任务 shell 即可；它不是 Conda 环境，不改变训练环境 Python 包。

运行库搜索路径包括前缀下：

```text
usr/lib/x86_64-linux-gnu
usr/lib/x86_64-linux-gnu/blas
usr/lib/x86_64-linux-gnu/lapack
usr/lib/x86_64-linux-gnu/pulseaudio
```

这里使用 Ubuntu 包自带的 SONAME 链接及实际库目录，**没有拿 PyAV 带哈希的私有库手工冒充 FFmpeg SONAME**。未复制/替换系统 libc、libstdc++ 或 NVIDIA 驱动库。

## 来源与四个旧包的处理

依赖初始计划来自 H20 已有 Ubuntu Jammy apt 索引；`python-apt` 仅在内存中求依赖，没有提交系统安装。包文件经 HTTPS 从 Ubuntu 官方归档下载。

其中 4 个旧索引条目已从公开 pool 下架。为避免降级为不明二进制，本次在**个人 staging** 下载当前 `jammy-updates` 的 `InRelease` 与 `main/binary-amd64/Packages.xz`：

1. 使用 H20 已有 `/usr/share/keyrings/ubuntu-archive-keyring.gpg` 与 `gpgv` 验证 InRelease；结果为 Ubuntu Archive Automatic Signing Key (2018) 的有效签名。
2. 按已签名 InRelease 的 SHA256/大小核验 Packages.xz。
3. 仅用同一 Jammy 发行版的更新条目替换 4 个缺失包，下载后再核验各自 SHA256。

| 包 | 本次采用版本 |
| --- | --- |
| libgfortran5 | `12.3.0-1ubuntu1~22.04.3` |
| libopenjp2-7 | `2.4.0-6ubuntu0.5` |
| libssh-gcrypt-4 | `0.9.6-2ubuntu0.22.04.8` |
| libvpx7 | `1.11.0-2ubuntu2.5` |

这是用户态运行库提取，不是可由系统 apt 管理的一次整机安装。包管理层的精确依赖关系不等于创建了一套独立操作系统；运行时仍依赖 H20 现有 Ubuntu 22.04 的 libc 等组件。因此不能把此目录任意复制到不同发行版/架构后宣称可用。已通过最终 ldd 和实际编解码验证当前 H20 组合。

FFmpeg 主版本 4 在 [TorchCodec v0.3.0 官方 README](https://github.com/meta-pytorch/torchcodec/tree/v0.3.0#installing-torchcodec) 声明支持的 4–7 范围内。这里记录兼容的已验收版本，不宣称是 FFmpeg 最新版。

## 验收文件与复现代码

- [完整包来源、版本、SHA256 清单](package-manifest.json)
- [Ubuntu 更新元数据、签名结果与四包清单](updated-package-metadata.json)
- [解包及版本回执](provision-receipt.json)：其中初次 ldd 使用原子发布前的临时构建目录，目录随后已重命名；不是运行时使用的最终路径。
- [最终路径与 CPU 编解码验收](smoke.json)：含最终路径 ldd、样本 SHA256、帧数和系统 dpkg 状态指纹。
- [用户态部署脚本](provision_user_ffmpeg.py)：已有前缀时拒绝覆盖；不需要重复运行。
- [私有目录元数据更新脚本](refresh_missing_metadata.py)：只刷新个人 staging，不操作系统 apt 列表。
- [CPU 视频验收脚本](smoke_user_ffmpeg.py)：默认保留已有验收视频，不覆盖；重测应为新样本指定另一文件名。

H20 下载及验收目录：`/u01/user/staging/ffmpeg-4.4.2-jammy/`。其中 `smoke-64x64-8frames.mp4` 是人工合成的小样本，可供后续 TorchCodec CPU 解码验收，不含业务数据。
