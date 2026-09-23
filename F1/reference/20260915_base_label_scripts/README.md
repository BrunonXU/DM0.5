# 用户提供的底盘补标脚本

2026-09-15从对话附件原样保存，两份文件保留原始字节与换行，来源和SHA256见[manifest.json](manifest.json)。未执行原脚本main；它们包含Windows批量处理路径，不作为当前生产入口。

- [detect_motion_windows.py](detect_motion_windows.py)：头部图像估计去返程窗口。
- [bulk_write_move_state.py](bulk_write_move_state.py)：按窗口构造六段路线，插入后补`/move/State`。

用户提供的源码是本次分析依据；无法仅据附件证明当时执行环境/版本。其切段函数在775份既有JSON上逐条完全重现原配方。源码审查、反例及修复方案见[任务审计](../../foam_sort_whole_body/results/base_script_audit_20260915/report.md)。
