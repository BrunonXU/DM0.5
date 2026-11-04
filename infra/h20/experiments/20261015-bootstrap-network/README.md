# H20 环境准备：下载渠道实测

> 采样日期：2026-10-15（北京时间），约 11:24–11:29。
> 本页只记录网络与固定文件下载验收，不表示训练环境或模型已安装完成。

## 当前可采用的渠道

**H20 直接访问本轮全部外部测试主机失败；经过已有公司代理 `http://192.0.2.34:3128`，清华 PyPI 和官方 OpenPI GCS 元数据可访问。** 不能继续沿用 9 月“公司代理不可用”的旧结论。

- H20 Python 包首选：命令级公司代理 + `https://pypi.tuna.tsinghua.edu.cn/simple`。清华两次完整固定 wheel 下载为 **5.24、4.10 MiB/s**，均匹配官方 SHA256。
- H20 模型渠道：官方 `storage.googleapis.com/openpi-assets/...` 的 `pi05_base/params/_METADATA` 已返回 **HTTP 200**，复测 **0.651 秒**。这只验证小型元数据，不能据此推断几十 GB 模型的持续速度和完整下载结果。
- 6000D 备用获取路径：官方 PyPI 直连固定 wheel **4.17 MiB/s**；可下载后通过已验收的 SSH/rsync 送到 H20。大文件不经过笔记本。
- 本轮没有修改任何机器的 `.bashrc`、全局 pip 配置、SSH 转发或代理配置。H20 的 SSH 非交互环境当时没有继承 `http_proxy` 等变量；公司代理在单次测试命令中显式指定。

## 如何比较

固定文件为 `numpy-1.26.4-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`，大小 **18,252,005 bytes**（17.41 MiB）。它只是测速样本，不代表积木训练 recipe 必须采用 NumPy 1.26.4 或 Python 3.11。

官方元数据 SHA256：

```text
666dbfb6ec68962c033a450943ded891bed2d54e6755e35e5835d63f4f6931d5
```

[官方文件元数据](official-artifact.json) 由 6000D 通过 PyPI HTTPS 取得。各镜像先读取索引，再下载同名文件；成功标准包括 curl 成功、完整大小和官方 SHA256 同时匹配。失败下载保留实际字节数及错误，不把不完整文件的平均吞吐混入“可用源速度排名”。下载临时文件在测试结束清理。

- 单次索引请求最多 10 秒；wheel 最多 20 秒 / 20 MiB，最多两个源并发。
- 首轮速度可能受并发和同一时段其他下载影响；H20 清华/官方补做单线程复测。
- 这是小文件采样，不是长期带宽承诺，也不证明所有包在镜像都有相同版本。
- 全程验证 HTTPS 证书，没有 `-k` / `trusted-host` 或降级 HTTP。

## H20 结果

| 渠道 | 直接访问 | 经公司代理 |
| --- | --- | --- |
| 官方 PyPI | TCP/连接超时 | 大索引 10 秒未取完；固定官方文件复测 20 秒只取到 3,771,257 bytes，未完整 |
| 清华 | 连接超时 | 两次完整 SHA256 一致，5.24 / 4.10 MiB/s |
| 阿里云 | 连接超时 | CONNECT 超时，未开始完整文件下载 |
| USTC | 连接超时 | CONNECT 超时，未开始完整文件下载 |
| 豆瓣旧地址 `pypi.doubanio.com` | 连接超时 | CONNECT 超时，未开始完整文件下载 |

上述“未开始下载”不能表述成镜像内容坏了或镜像服务器没有带宽；可能是当前网络/代理可达性、限时或服务状态问题。此次不选择豆瓣作为安装源，亦没有为其关闭 TLS 检查。

H20 模型/工具渠道 HEAD：

| 地址 | 公司代理结果 | 能证明什么 |
| --- | --- | --- |
| GitHub `astral-sh/uv/releases` | 200 | Release 页面可达；真实二进制常有额外重定向，需要下载单独验证 |
| GCS 根 URL | 400 | HTTPS 有回应；根地址不是具体模型文件 |
| OpenPI `pi05_base/params/_METADATA` | 两次 200，约 0.65 秒 | 此官方小文件路径可达 |
| HF / hf-mirror 的尝试 API 路径 | 超时或 401 | 未验证可下载目标模型；401 也可能是不存在/受限仓库，不能凭它判定整个站不可达或需要分享 Token |
| PyTorch cu128 索引 | 一次 TLS 超时，第二次 200 | 索引可达性有波动，尚不能据 HEAD 宣称 CUDA wheel 全部可用 |

## 6000D 对照结果

6000D 当时已有 `127.0.0.1:7890` HTTP 代理；本轮分别测试绕过代理的直接访问、继承现有代理两种方式。

| 同一完整 wheel | 6000D 直连 MiB/s | 6000D 现有代理 MiB/s |
| --- | ---: | ---: |
| 官方 PyPI | 4.17 | TLS 超时，未完整 |
| 清华 | 2.76 | 1.80 |
| USTC | 2.50 | 1.78 |
| 阿里云 | 20 秒仅 13.86 MB，未完整 | 1.56 |
| 豆瓣旧地址 | TLS/连接 reset | TLS EOF |

USTC 官方说明其索引由 TUNA 同步，部分文件会重定向 TUNA；不能把它视作完全独立的第三份上游副本。6000D 结果不能直接套用到 H20。

## 复测

在 **6000D /home/user/projects** 执行；脚本只使用 Python 标准库和 curl，不安装依赖。

```bash
# H20 经公司代理：单线程复测清华；结果打印 JSON
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes h20-train \
  'python3 - --mode company-proxy --source tsinghua --workers 1 --skip-channels' \
  < infra/h20/experiments/20261015-bootstrap-network/probe.py

# 6000D 官方直连，先索引再完整固定 wheel
python3 infra/h20/experiments/20261015-bootstrap-network/probe.py \
  --mode direct --source pypi --workers 1 --skip-channels
```

安装阶段应在相应任务进程中显式设置代理/包索引，锁定目标依赖版本并记录来源；不把多个镜像混成无限制 `extra-index-url`，也不让测试脚本自动安装样本包。

## 证据与来源

- [完整测速脚本](probe.py)
- [H20 直连](h20-direct.json)
- [H20 公司代理 HEAD 初测](h20-company-proxy.json)
- [H20 公司代理五源完整测试](h20-company-proxy-full.json)
- [H20 官方固定文件和清华单线程复测](h20-company-proxy-repeat.json)
- [H20 OpenPI 官方元数据复测](h20-openpi-metadata.json)
- [6000D 直连](6000d-direct.json)
- [6000D 继承代理](6000d-environment.json)
- 官方说明：[PyPI NumPy 1.26.4](https://pypi.org/project/numpy/1.26.4/)、[清华 PyPI 镜像](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)、[USTC PyPI 镜像](https://mirrors.ustc.edu.cn/help/pypi.html)、[阿里云 PyPI 镜像](https://developer.aliyun.com/mirror/pypi)。本次对阿里仍使用 HTTPS，不采用文档中的旧 HTTP/trusted-host 写法。

## 后续安装发现：PyTorch 官方文件域名

安装阶段实际发现：`download.pytorch.org/whl/cu128` 索引中的目标 wheel 指向 `download-r2.pytorch.org`，H20 公司代理到后者请求失败。索引 HTTP 200 不代表这些文件域名也可达。

2026-10-15 后续复测，同样文件路径使用 **`download.pytorch.org`** 时，H20 公司代理对 torch、torchvision 的 wheel 和 `.metadata` 共四个 HEAD 都返回 **200，且未重定向**。因此可把这两项固定依赖写成下列官方直链，并保留官方索引公布的 SHA256；安装时仍必须核验完整文件哈希。

```text
https://download.pytorch.org/whl/cu128/torch-2.7.1%2Bcu128-cp311-cp311-manylinux_2_28_x86_64.whl#sha256=c301dc280458afd95450af794924c98fe07522dd148ff384739b810e3e3179f2
https://download.pytorch.org/whl/cu128/torchvision-0.22.1%2Bcu128-cp311-cp311-manylinux_2_28_x86_64.whl#sha256=92568ac46b13a8c88b61589800b1b9c4629be091ea7ce080fc6fc622e11e0915
```

这一步没有绕过证书检查，没有把 PyTorch 包替换成不明第三方构建，也没有安装依赖。仅靠 HEAD 不能证明完整二进制下载完成，实际安装验收在环境记录中另行保存。

证据：[官方索引提取](torch-index-initial.json)、[H20 替换官方文件域名后的 HEAD 复测](torch-channel.json)。
