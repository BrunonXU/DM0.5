# 自动测试

这里仅放不连接真实G2、不发送Servo的pytest测试。终端执行：

```
PYTHONPATH=. python3 -m pytest -q tests
```

需要真机的实验必须放在`g2_sort/operations/hardware_tests/`，不得以`test_*.py`
命名放入本目录。
