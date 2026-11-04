"""One model/profile selector; recovery code is independent of checkpoint.

The deployment registry identifies verified server/client pairs. No fallback
silently drops requested recovery. This launcher never authorizes robot motion.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen


def verify_profile(profile,health):
    if health.get('ready') is not True or health.get('protocol')!='f1_eef_rtc_v2':
        raise ValueError('推理服务未就绪或RTC协议不匹配')
    if health.get('model_id')!=profile['model_id']:
        raise ValueError('实际模型与所选模型不一致；拒绝启动')
    recovery=health.get('ik_recovery',{})
    enabled=recovery.get('enabled') is True
    if profile['recovery']=='standard':
        if enabled:raise ValueError('服务已开启重采样，不能标记为standard')
        return dict(model_id=health['model_id'],model_version=health['model_version'],
            recovery='standard',server_resampling=False,client_feasible_prefix=False,
            client_tracking_hold=False,client_version=profile['client_version'])
    expected=dict(enabled=True,wrapper='f1_strict_ik_recovery_v1',eef_postprocessed=False,
        ranges_changed=False,may_select_resampled_prediction=True,
        partial_response='standard_ik_rejection_requires_opt_in_client')
    if any(recovery.get(k)!=v for k,v in expected.items()):
        raise ValueError('所选服务没有完整IK恢复能力；不会静默降级到普通推理')
    if profile.get('client_tracking_hold') is not True:
        raise ValueError('客户端恢复能力未登记验收')
    return dict(model_id=health['model_id'],model_version=health['model_version'],
        recovery='ik',server_resampling=True,client_feasible_prefix=True,client_tracking_hold=True,
        client_version=profile['client_version'],server_wrapper=recovery['wrapper'],
        server_wrapper_sha256=recovery['wrapper_sha256'],max_attempts=recovery['max_attempts'],
        selection_budget_ms=recovery['budget_ms'],eef_postprocessed=False,ranges_changed=False)


def main():
    # Historical import compatibility for verify_profile; no independent selector.
    entry=os.environ.get('F1_INFERENCE_ENTRY',
        '/home/user/Bruno/DAgger-runtime/inference-current/inference_runtime.py')
    os.execvp('python3',['python3',entry,*sys.argv[1:]])


if __name__=='__main__':main()
