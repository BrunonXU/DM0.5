"""Reject physical reports; accept explicitly evidenced memory-only RPC fixtures."""


def require_offline_output_report(report, events, outputs):
    if (report.get('physical_motion') is not False or report.get('robot_connected') is not False
            or report.get('scenario', {}).get('follower') != 'approved-output'):
        raise ValueError('not_approved_output_replay')
    if not outputs or any(row.get('simulation') is not True for row in outputs):
        raise ValueError('offline_output_provenance_missing')
    if report.get('mode') == 'fake':
        return
    fixture = report.get('offline_hardware_contract_fixture', {})
    identity = report.get('final_status', {}).get('recording_identity') or {}
    markers = [row for row in events if row.get('kind') == 'offline_hardware_contract_fixture']
    if (report.get('mode') != 'hardware' or fixture.get('actuator_transport') != 'MemoryTransport'
            or identity.get('offline_fixture') is not True or identity.get('actuator_transport') != 'memory'
            or not markers or any(row.get('simulation') is not True for row in events)
            or any(row.get('physical_motion') is not False or row.get('actuator_transport') != 'memory'
                   for row in markers)):
        raise ValueError('hardware_report_without_memory_fixture_evidence')
