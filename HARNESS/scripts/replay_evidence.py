"""Describe offline execution scope without rewriting native result annotations."""


def replay_provenance(mode, outcomes, events, publications, owner_outputs):
    """A failed native result can retain synthetic=False inside a fake run.

    Preserve it as observed. Execution provenance is separately checked against
    the fake engine, tagged event stream and mock output records. This summary
    does not authorize experience promotion or claim physical task success.
    """
    all_results_synthetic = bool(outcomes) and all(
        row['result'].get('synthetic') is True for row in outcomes)
    checks = dict(
        fake_engine=mode == 'fake',
        simulation_events=bool(events) and all(row.get('simulation') is True for row in events),
        simulation_publications=all(row.get('simulation') is True for row in publications),
        simulation_owner_outputs=all(row.get('simulation') is True for row in owner_outputs),
    )
    return dict(
        all_results_synthetic=all_results_synthetic,
        result_annotation_missing_or_false=[row['skill'] for row in outcomes
            if row['result'].get('synthetic') is not True],
        simulation_scope_confirmed=all(checks.values()),
        scope_checks=checks,
        real_experience_eligible=False,
    )
