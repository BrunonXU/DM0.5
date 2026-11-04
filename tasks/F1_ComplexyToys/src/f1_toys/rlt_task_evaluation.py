"""Task-level, prospective comparisons; no model ranking from imitation loss."""
import math
import random

STRATEGIES = ('b1250', 'bc400', 'rlt')


def preregister(batch_id, identities, seed=44):
    if set(identities) != set(STRATEGIES) or any(not identities[s] for s in STRATEGIES):
        raise ValueError('All three fixed model identities are required')
    rng = random.Random(seed)
    attempts = []
    for group in range(1, 5):
        order = list(STRATEGIES); rng.shuffle(order)
        for strategy in order:
            attempts.append(dict(attempt_id=f'{batch_id}-{len(attempts)+1:02}', start_group=str(group),
                                 strategy=strategy, model_identity=identities[strategy], budget_seconds=30))
    return dict(schema='f1_task_comparison_v1', batch_id=batch_id, seed=seed,
                collection_purpose='evaluation', fixed_weights=True, attempts=attempts,
                primary='autonomous_place_release_success', scope='pilot_not_confirmatory')


def preregister_paired(batch_id, identities, reference, seed=45):
    """One prospective fixed 20-pair test; no optional stopping/picked reference."""
    if reference not in ('b1250', 'bc400') or set(identities) != {'rlt', reference}:
        raise ValueError('Choose one fixed reference and candidate before collecting')
    if any(not v for v in identities.values()):
        raise ValueError('Fixed identities required')
    rng = random.Random(seed); attempts = []
    for group in range(1, 21):
        order = ['rlt', reference]; rng.shuffle(order)
        for strategy in order:
            attempts.append(dict(attempt_id=f'{batch_id}-{len(attempts)+1:02}',start_group=str(group),
                                 strategy=strategy, model_identity=identities[strategy], budget_seconds=30))
    return dict(schema='f1_task_comparison_v1',batch_id=batch_id,seed=seed,
        collection_purpose='evaluation',fixed_weights=True,attempts=attempts,
        primary='autonomous_place_release_success',scope='fixed_20_pairs',reference=reference,
        stopping_rule='complete_all_40_started_attempts',alpha=.05,
        test='exact_two_sided_mcnemar',claim_scope='only_the_preregistered_reference')


def wilson(successes, total):
    if not total:
        return None
    z = 1.959963984540054
    p = successes/total; den = 1+z*z/total
    center = (p+z*z/(2*total))/den
    radius = z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/den
    return [max(0., center-radius), min(1., center+radius)]


def paired(candidate, reference):
    if set(candidate) != set(reference):
        raise ValueError('Paired outcomes require the same pre-registered groups')
    wins = sum(candidate[k] and not reference[k] for k in candidate)
    losses = sum(reference[k] and not candidate[k] for k in candidate)
    discordant = wins+losses
    p = min(1., 2*sum(math.comb(discordant, i) for i in range(min(wins, losses)+1))/2**discordant)
    return dict(wins=wins, losses=losses, ties=len(candidate)-discordant,
                exact_two_sided_mcnemar_p=p, pairs=len(candidate))


def summarize(protocol, results):
    expected = {r['attempt_id']: r for r in protocol['attempts']}
    strategies = tuple(s for s in STRATEGIES if any(p['strategy']==s for p in protocol['attempts']))
    seen = set(); groups = {s: {} for s in strategies}; output = {}
    for row in results:
        key = row['attempt_id']
        if key in seen or key not in expected:
            raise ValueError('Duplicate or unregistered evaluation attempt')
        seen.add(key); plan = expected[key]
        if row['model_identity'] != plan['model_identity'] or row['strategy'] != plan['strategy']:
            raise ValueError('Evaluation weights or strategy changed')
        if row.get('reviewed') is not True:
            raise ValueError('Task results need explicit review, not a gripper proxy')
        if row['outcome'] not in ('success', 'failure', 'timeout', 'unknown'):
            raise ValueError('Invalid task outcome')
        # Infrastructure interruptions remain in all-starts denominator. They
        # are not silently reported as strategy failures or autonomous successes.
        eligible = row.get('infrastructure_interrupted') is False and row['outcome'] != 'unknown'
        if eligible:
            groups[plan['strategy']][plan['start_group']] = bool(
                row['outcome'] == 'success' and not row['human_intervened'])
    for strategy in strategies:
        rows = [r for r in results if r['strategy'] == strategy]
        valid = [r for r in rows if not r['infrastructure_interrupted'] and r['outcome'] != 'unknown']
        success = sum(r['outcome'] == 'success' and not r['human_intervened'] for r in valid)
        output[strategy] = dict(started=len(rows), policy_evaluable=len(valid), autonomous_successes=success,
            autonomous_success_all_starts=None if not rows else success/len(rows),
            autonomous_success_rate=None if not valid else success/len(valid),
            descriptive_wilson95=wilson(success, len(valid)),
            assisted_successes=sum(r['outcome']=='success' and r['human_intervened'] for r in valid),
            infrastructure_interruptions=sum(r['infrastructure_interrupted'] for r in rows),
            failures=sum(r['outcome']=='failure' for r in valid),
            timeouts=sum(r['outcome']=='timeout' for r in valid),
            attempts=rows)
    comparisons = {}
    for reference in (s for s in strategies if s != 'rlt'):
        common = set(groups['rlt']) & set(groups[reference])
        comparisons[reference] = paired({k:groups['rlt'][k] for k in common}, {k:groups[reference][k] for k in common})
        comparisons[reference]['excluded_unpaired_groups'] = sorted(
            (set(groups['rlt']) | set(groups[reference]))-common)
    missing = sorted(set(expected)-seen)
    claim = False
    if protocol['scope'] == 'fixed_20_pairs':
        comparison = comparisons[protocol['reference']]
        claim = (not missing and comparison['pairs'] == 20 and comparison['wins'] > comparison['losses']
                 and comparison['exact_two_sided_mcnemar_p'] < protocol['alpha'])
    return dict(scope=protocol['scope'], strategies=output, paired=comparisons,
                missing_attempts=missing, improvement_claim=claim,
                note=('Pilot is descriptive.' if protocol['scope']=='pilot_not_confirmatory' else
                      'Claim applies only to the fixed reference and this test setting; incomplete/infra-excluded pairs cannot establish improvement.'))
