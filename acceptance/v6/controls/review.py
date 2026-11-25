"""Prepare a read-only scientific review from an independently anchored controls export."""
import argparse
import json
from pathlib import Path
import sys

from fixture import V6, file_sha as file_digest, read, save

sys.path.insert(0, str(V6))
import verify


def comparisons(initial, packet):
    """Recompute coverage and paired tolerance calls directly from each injected measurement."""
    limits = initial['measurement_contract']
    precision = limits['tolerances']
    results = []
    for observation in packet['observations']:
        value = observation['measurement']
        if 'chains' not in value:
            results.append({'observation': observation['id'], 'metadata': value})
            continue
        covered = (value['matched_positions'] >= limits['minimum_positions'] and
                   value['fraction_of_observed_union'] >= limits['minimum_fraction_of_observed_union'])
        results.append({'observation':observation['id'], 'chain_pair':value['chains'],
                        'coverage_pass':covered, 'angle_within_tolerance':
                        value['abs_angle_change_deg'] <= precision['max_abs_angle_change_deg'],
                        'rmsd_within_tolerance':value['abs_rmsd_change_angstrom'] <= precision['max_abs_rmsd_change_angstrom'],
                        'source_values':value})
    return results


def dossier(bundle, anchor):
    root = bundle.resolve()
    anchor = anchor.resolve()
    if root in anchor.parents or file_digest(root/'bundle.json') != read(anchor)['bundle_sha256']:
        raise ValueError('The bundle does not match a detached reviewer anchor')
    contents = read(root/'bundle.json')
    if contents['mode'] != 'controls':
        raise ValueError('Select a matched controls export')
    verify.verify_objects(root,contents)
    try:
        verify.verify(root,anchor)
        series = {'status':'PASS'}
    except ValueError as error:
        series = {'status':'FAIL','reason':str(error)}
    results = []
    suite = read(V6/'reviewer/suite.json')
    for run in contents['runs']:
        try:
            verify.verify_run(root,run,suite,'controls')
            finding = {'status':'PASS'}
        except ValueError as error:
            finding = {'status':'FAIL','reason':str(error)}
        def get(key):
            return verify.document(root, run[key])
        initial, packet = get('initial'), get('packet')
        before, after = get('before'), get('settled')
        turn, adoption = get('turn'), get('commit')
        intent = verify.document(root,turn['tool_arguments'])
        if isinstance(intent,str):
            intent=json.loads(intent)
        results.append({'registered_checks':finding,'run_id':run['run_id'],'native_thread_id':turn['thread_id'],'native_turn_id':turn['turn_id'],
            'decision_id':turn['decision_id'],'source':'published synthetic inputs','observation_packet':packet['packet_id'],
            'independent_numeric_check':comparisons(initial,packet),'cited_evidence':intent['evidence_refs'],
            'submitted_reason':intent['rationale'],'reconsider_when':intent['reconsider_when'],
            'accepted_operations':intent['ops'],'planned_dispatches':adoption['dispatches'],
            'claim_before':before['science'],'claim_after':after['science'],
            'job_submissions_before':before['spent_job_submissions'],'job_submissions_after':after['spent_job_submissions'],
            'protected_jobs':[{k:job[k] for k in ('job_id','operation_key','branch','status')}
                              for job in after['jobs'] if job['branch']=='citation-index']})
    return {'verifier':'V6 detached-anchor source and consequence checks','version':1,'registered_series':series,'runs':results,
            'scientific_review':'pending independent interpretation against reviewer/rubric.md'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('bundle',type=Path)
    p.add_argument('--anchor',required=True,type=Path)
    p.add_argument('--out',required=True,type=Path)
    args=p.parse_args()
    if args.out.exists():
        p.error('Choose a new report path')
    report=dossier(args.bundle,args.anchor)
    save(args.out,report)
    sys.exit(0 if report['registered_series']['status']=='PASS' else 1)
