# `g2_sort` 包边界

- `config/`：34D、prompt、三相机和H5复位目标的唯一合同。
- `deployment/`：正式DM0.5 G2客户端；不包含硬件实验。
- `operations/diagnostics/`：只读GDK、相机、HTTP和已锁定hold诊断。
- `operations/recovery/`：旧H5姿态只读预览及批准的短时Split Home复位。
- `operations/hardware_tests/`：dry-run接口实验；危险execute已锁定。

自动测试位于项目根目录`tests/`，不属于Python运行包，也不能连接真实GDK。
