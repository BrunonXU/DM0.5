"""Typed F1 availability faults; none authorize unchecked motion."""


class FeedbackUnavailable(RuntimeError):
    """A required physical measurement is temporarily missing or stale."""


class TargetExpired(RuntimeError):
    """An unexecuted target is too old; discard and re-authorize."""


class ManualReferenceChanged(RuntimeError):
    """Manual IK must release its reference before another candidate."""


class RecordingUnavailable(RuntimeError):
    """Acquisition evidence is lost; automatic motion must stop."""


def transient_control_reason(reason):
    return (reason in ('operator_watchdog', 'operator_watchdog_before_publication',
                       'operator_transport_stale', 'controller_disconnected',
                       'operator_disconnected', 'operator_session_changed')
            or reason.startswith(('operator_transport_error:', 'policy_failed:',
                                  'rtc_', 'ik_rejected:', 'camera_unavailable:',
                                  'runtime_wait:')))
