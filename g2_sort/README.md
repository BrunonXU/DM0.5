# G2 分拣项目代码

该目录只存放G2任务专用代码，不属于OpenDM上游。

- `config/`：冻结的34D动作合同；
- `training/`：三路RGB、34D absolute action的LoRA recipe；
- `data_tools/`：归档索引、H5审计、转换、split和动态分析；
- `deployment/`：当前仅允许shadow mode的推理客户端；
- `validation/`：checkpoint交付合同检查；
- `tests/`：34D动作头和episode划分回归测试。

运行模块时从项目根目录设置：

```bash
export PYTHONPATH="$PWD:$PWD/opendm"
```

原始数据转换必须在本地NVMe执行，不直接从SMB共享盘训练或写回共享盘。
