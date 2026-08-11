# Operations

与正式VLA客户端分离的现场工具：

- `diagnostics/`：预检和只读状态检查；危险hold执行已软件锁定。
- `recovery/`：H5初始姿态提取、复位预览，以及唯一批准的短时Split Home复位。
- `hardware_tests/`：默认dry-run的接口实验；当前危险execute均锁定。

操作者应优先通过项目根目录的`scripts/`调用这些工具。

允许执行的真实动作统一输入`ok/OK`确认并倒数5秒，不使用长确认词。现场唯一复位
入口是`scripts/03_reset_home.sh --execute`；旧H5预览模块和危险硬件实验仍由代码
执行锁保护。
