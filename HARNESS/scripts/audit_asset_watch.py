"""Local Linux change notification; invalidation only, never permission to act."""
import ctypes,os,sys


class AssetWatch:
    def __init__(self,paths):
        self.fd=None;self.dirty=False
        try:
            if sys.platform!='linux':return
            libc=ctypes.CDLL(None,use_errno=True)
            libc.inotify_init1.argtypes=[ctypes.c_int];libc.inotify_init1.restype=ctypes.c_int
            libc.inotify_add_watch.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_uint32]
            libc.inotify_add_watch.restype=ctypes.c_int
            fd=libc.inotify_init1(os.O_NONBLOCK|os.O_CLOEXEC)
            if fd<0:return
            self.fd=fd
            # MODIFY, ATTRIB, CLOSE_WRITE, MOVED_FROM/TO, CREATE, DELETE,
            # DELETE_SELF, MOVE_SELF, UNMOUNT. Overflow/ignored also appear.
            mask=0x00000002|0x00000004|0x00000008|0x00000040|0x00000080|0x00000100|0x00000200|0x00000400|0x00000800|0x00002000
            for p in paths:
                if libc.inotify_add_watch(fd,os.fsencode(p),mask)<0:
                    self.close();return
        except (OSError,AttributeError):self.close()

    def changed(self):
        if self.dirty:return True
        if self.fd is None:return False  # Caller must use full content checks.
        try:
            self.dirty=bool(os.read(self.fd,65536))
        except BlockingIOError:return False
        except OSError:self.dirty=True
        return self.dirty

    def close(self):
        fd,self.fd=self.fd,None
        if fd is not None:os.close(fd)

    def __del__(self):
        self.close()
