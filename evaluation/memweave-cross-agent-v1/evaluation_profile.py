"""Explicit scoring contracts; the frozen v1 dataset is never rewritten."""
from copy import deepcopy

PROFILES = ('shadow-v1', 'on-demand-v2')


def expected_for(case, profile):
    if profile not in PROFILES:
        raise ValueError(f'unknown evaluation profile: {profile}')
    expected = deepcopy(case['expected'])
    if profile == 'on-demand-v2' and case['category'] == 'lfhv_shadow_recovery':
        eligible = [row['id'] for row in case['memory_records']
                    if row['status'] == 'archived' and not row.get('superseded_by')
                    and (row['scope'] == 'user' or row['project_key'] == case['project_context'])]
        expected.update(decision='inject', evidence_ids=eligible,
                        forbidden_ids=[key for key in expected['forbidden_ids'] if key not in eligible],
                        shadow_probe='none', restored_ids=eligible)
    return expected
