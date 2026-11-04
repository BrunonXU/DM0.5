"""Check the actual opt-in entry reaches CLI's locally imported classes."""
from pathlib import Path
import runpy


def test_entry_binds_the_modules_imported_inside_cli_main(monkeypatch):
    from f1dagger import rtc, runtime
    from f1dagger.rtc_prefix_recovery import RTCFeasiblePrefixEngine
    from f1dagger.rtc_prefix_runtime import PrefixRecoveryControlLoop
    # Register originals for cleanup; importing the entry enables no hardware.
    monkeypatch.setattr(rtc, 'RTCPolicyEngine', rtc.RTCPolicyEngine)
    monkeypatch.setattr(runtime, 'ControlLoop', runtime.ControlLoop)
    script=Path(__file__).resolve().parents[3]/'tasks/F1_ComplexyToys/scripts/run_f1_ik_recovery.py'
    runpy.run_path(str(script), run_name='entry_binding_test')
    # These are the exact import sites executed by cli.main after preflight.
    from f1dagger.rtc import RTCPolicyEngine
    from f1dagger.runtime import ControlLoop
    assert RTCPolicyEngine is RTCFeasiblePrefixEngine
    assert ControlLoop is PrefixRecoveryControlLoop


def test_tracking_entry_explicitly_enables_only_the_independent_candidate(monkeypatch):
    from f1dagger import rtc,runtime
    from f1dagger.rtc_prefix_recovery import RTCFeasiblePrefixEngine
    monkeypatch.setattr(rtc,'RTCPolicyEngine',rtc.RTCPolicyEngine)
    monkeypatch.setattr(runtime,'ControlLoop',runtime.ControlLoop)
    script=Path(__file__).resolve().parents[3]/'tasks/F1_ComplexyToys/scripts/run_f1_ik_tracking_recovery.py'
    entry=runpy.run_path(str(script),run_name='tracking_entry_binding_test')
    cls=entry['RTCTrackingHoldRecoveryEngine']
    assert rtc.RTCPolicyEngine is cls
    seen={}
    monkeypatch.setattr(RTCFeasiblePrefixEngine,'__init__',lambda self,*args,**kwargs:seen.update(kwargs))
    cls()
    assert seen['tracking_hold_recovery_enabled'] is True
