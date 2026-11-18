"""Linux lifecycle wrapper for trusted, same-UID numerical workers.

Run before numerical imports, then exec without changing PID or inherited IPC.
The parent must spawn from its main thread: Linux's parent-death signal is
bound to the creating thread, not the lifetime of all parent process threads.
No preexec_fn is used in the potentially multithreaded parent.
"""
import ctypes
import os
import signal
import sys

PR_SET_PDEATHSIG = 1
PR_GET_PDEATHSIG = 2


def _prctl(option, value):
    if sys.platform != 'linux':
        raise RuntimeError('audit_parent_lifecycle_requires_linux')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(ctypes.c_int(option), value, ctypes.c_ulong(0),
                  ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def verify_parent_death_signal():
    value = ctypes.c_int()
    _prctl(PR_GET_PDEATHSIG, ctypes.byref(value))
    if value.value != signal.SIGKILL:
        raise RuntimeError('audit_parent_death_signal_missing')


def launch(parent_pid, command):
    if type(parent_pid) is not int or parent_pid <= 1 or not command:
        raise ValueError('invalid_audit_parent_or_command')
    _prctl(PR_SET_PDEATHSIG, ctypes.c_ulong(signal.SIGKILL))
    # Covers parent exit before registration. Exit after this check is handled
    # by the kernel even while native code holds the GIL or the worker is STOPped.
    if os.getppid() != parent_pid:
        raise RuntimeError('audit_parent_changed_before_launch')
    verify_parent_death_signal()
    os.execv(command[0], command)


if __name__ == '__main__':
    if len(sys.argv) < 4 or sys.argv[2] != '--':
        raise SystemExit('internal numerical worker lifecycle wrapper only')
    launch(int(sys.argv[1]), sys.argv[3:])
