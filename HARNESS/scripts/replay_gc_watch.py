"""Bounded GC timing evidence for the offline multi-run driver, no GC tuning."""
import gc,threading,time


class ReplayGCWatch:
    def __init__(self):self.rows=[];self.pending={};self.dropped=0

    def callback(self,phase,info):
        key=(threading.get_ident(),info['generation']);now=time.monotonic_ns()
        if phase=='start':self.pending[key]=now
        elif phase=='stop':
            start=self.pending.pop(key,None)
            if start is not None:
                row=dict(thread_id=key[0],generation=key[1],start_ns=start,end_ns=now,
                         duration_ns=now-start,collected=info['collected'],uncollectable=info['uncollectable'])
                if len(self.rows)<2048:self.rows.append(row)
                else:self.dropped+=1

    def start(self):gc.callbacks.append(self.callback)
    def close(self):gc.callbacks.remove(self.callback)
