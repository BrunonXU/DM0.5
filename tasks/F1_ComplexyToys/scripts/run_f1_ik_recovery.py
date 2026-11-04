"""Explicit experimental F1 CLI entry; ordinary entrypoints are untouched."""
from f1dagger.rtc_prefix_recovery import RTCFeasiblePrefixEngine
from f1dagger.rtc_prefix_runtime import PrefixRecoveryControlLoop
from f1dagger import rtc, runtime, cli

rtc.RTCPolicyEngine=RTCFeasiblePrefixEngine
# cli.main imports ControlLoop locally; bind its source module, not a global
# attribute on cli that the local import would silently ignore.
runtime.ControlLoop=PrefixRecoveryControlLoop

if __name__=='__main__':
    cli.main()
