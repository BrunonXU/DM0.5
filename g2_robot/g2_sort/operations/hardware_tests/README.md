# Hardware Tests

这里是不会被pytest自动运行的真机接口实验。所有工具默认dry-run；有过危险
真机结果的execute已软件锁定。它们不是正常上机流程的一部分。

`o10_move_ee_smoke.py`曾用于隔离验证官方O10 `move_ee_pos`。由于重启后的零反馈
回发会触发官方“开手”命令并造成拇指靠近相机，当前只允许预览，不得执行。
