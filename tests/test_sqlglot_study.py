import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location('sqlglot_study', Path(__file__).parents[1]/'scripts/run-sqlglot-study.py')
study = importlib.util.module_from_spec(spec)
spec.loader.exec_module(study)


def test_latency_joins_successful_events_including_finalization(tmp_path):
    import json
    import yaml
    (tmp_path/'run-state.json').write_text(json.dumps({'state': 'failed'}))
    (tmp_path/'experiment.yaml').write_text(yaml.safe_dump({'execution': {
        'concurrency_levels': [3], 'arm_timeout_seconds': 120}}))
    events = tmp_path/'attempts/a/b/events'
    events.mkdir(parents=True)
    rows = []
    for sid in ('a', 'b', 'failed'):
        rows.append(dict(event='sandbox_create_gate_wait_started', session_id=sid, wall_time_ns=100*10**9))
    for sid, end in [('a', 200), ('b', 250), ('failed', 260)]:
        rows.append(dict(event='session_timing', session_id=sid, wall_time_ns=(end+1)*10**9,
                         time_spans=[dict(name='agent', end_unix_s=end)]))
        rows.append(dict(event='session_complete', session_id=sid, wall_time_ns=(end+2)*10**9,
                         valid=sid != 'failed'))
    (events/'events.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
    models=events.parent/'model-gateway'
    models.mkdir()
    (models/'a.json').write_text(json.dumps([dict(request_id='one',status_code=200,
        delivered=True,delivered_unix_s=150)]))
    (models/'a.deliveries.jsonl').write_text('\n'.join(json.dumps(dict(
        request_id=req,status_code=status,delivered_unix_s=150))
        for req,status in [('one',200),('two',200),('error',500)])+'\n{"partial":')
    result = study.analyze(tmp_path)
    assert result['successful_agents'] == 1
    assert result['successful_including_finalization'] == 2
    assert result['latency_samples'] == 2
    assert result['completed_only_p90_s'] == 150
    assert result['population_p90_s'] is None
    assert result['whole_window']['steps_per_s'] == 2/120
    assert result['event_counts']['session_complete'] == 3
    assert result['measurement_event_counts']['session_complete'] == 1
    (tmp_path/'run-state.json').write_text(json.dumps({'state':'succeeded'}))
    finished=study.analyze(tmp_path)
    assert finished['whole_window']['agents']==finished['successful_agents']==2


def test_stable_stop_rejects_startup_and_error_induced_plateau():
    s=dict(measurement_seconds=720,actual_arrivals=640,offered=640,
           seconds_since_last_creation=400,stable_last_360s=True,
           event_counts={'task_environment_checked':400})
    assert study.stable_stop_allowed(s)
    assert not study.stable_stop_allowed({**s,'seconds_since_last_creation':10})
    assert not study.stable_stop_allowed({**s,'actual_arrivals':639})
    assert not study.stable_stop_allowed({**s,'stable_last_360s':False})
    assert not study.stable_stop_allowed({**s,'event_counts':{
        'task_environment_checked':400,'session_failed':100}})


def test_pool_sizes_follow_measured_shared_envelope():
    sizes, reason = study.capacity_points({'peaks_bytes':{'shared_joint_physical_bytes':200*2**30,
                                                          'shared_borrow_reserved_bytes':120*2**30}},320)
    assert sizes == [128,192]
    assert '240.000' in reason
    assert study.capacity_points({'peaks_bytes':{}},320)[0] == []


def test_zero_success_does_not_fabricate_rate_anchor():
    assert study.measured_service_rate({'bins_120s':[], 'successful_agents':0})[0] is None


def test_stable_summary_excludes_shutdown_from_measurement(tmp_path, monkeypatch):
    final={'state':'aborted','cleanup_verified':True,'failure_category':'aborted',
           'event_counts':{'tool_completed':110}, 'mechanisms':{}, 'measurement_seconds':800}
    monkeypatch.setattr(study,'analyze',lambda run:final)
    run=tmp_path/'test-run'
    study.save(tmp_path/'test-run.stable-stop.json',dict(measurement_seconds=720,
        state='running',event_counts={'tool_completed':100}))
    result=study.measured_summary(run,tmp_path)
    assert result['measurement_seconds']==720
    assert result['event_counts']['tool_completed']==100
    assert result['finalization']['event_counts']['tool_completed']==110
    assert result['cleanup_verified']


def test_fast_plan_preserves_live_run_and_never_duplicates_points():
    plan={'pressure640_fast_plan':True,'rules':{},'order':[
        {'kind':'full-c128','state':'waiting_for_existing_run'},
        {'kind':'ledger-c128','state':'pending'}]}
    study.configure_pressure_plan(plan)
    deadline=plan['deadline_unix_s']
    study.configure_pressure_plan(plan)
    assert len(plan['order'])==9
    assert plan['order'][0]['state']=='waiting_for_existing_run'
    assert plan['order'][1]['state']=='deferred'
    assert plan['order'][2]['kind']=='pressure640-full'
    assert plan['deadline_unix_s']==deadline


def test_pressure_study_keeps_matched_non_target_configuration():
    base={'environment':{'resources':{'compute_nodes':[{},{}],
            'warm_memory_capacity_mib':327680, 'shared_memory_borrow_limit_mib':163840}},
          'mechanisms':{'shared_memory':{'borrow':'pressure'},
            'checkpoint':{'storage':'tiered','residency':'value'},
            'idle_reclaim':{'policy':'wait'},
            'placement':{'policy':'load_aware','template_affinity':True,'tool_call_cpu_policy':'c2'},
            'runtime':{'memory_protection':True,'cpu_release_boost':True,'proactive_memory_reclaim':True}},
          'execution':{'random_seed':42,'poisson_arrival_window_fraction':.25}}
    full=study.make_spec(base,'pressure640-full')
    for variant in ('no-borrow','no-checkpoint','no-runtime','round-robin','no-llc'):
        changed=study.make_spec(base,'pressure640-'+variant)
        assert changed['environment']==full['environment']
        assert changed['execution']==full['execution']
        different=[k for k in full['mechanisms'] if changed['mechanisms'][k]!=full['mechanisms'][k]]
        assert len(different)==1
    assert full['environment']['resources']['local_memory_capacity_mib']==448*1024
    assert full['execution']['concurrency_levels']==[640]
    assert full['execution']['arm_timeout_seconds']==900
    off=study.make_spec(base,'pressure640-no-pool')
    assert off['mechanisms']['checkpoint']['storage']=='disk'


def test_user_latency_priority_defers_escalation_without_losing_rows():
    plan = {'prefer_low_scale_before_escalation': True, 'rules': {}, 'order': [
        {'kind': kind, 'state': 'pending'} for kind in
        ('full-2560', 'full-4096', 'no-pool-4096', 'no-pool-2560',
         'full-c128', 'ledger-c128', 'full-c64', 'ledger-c64', 'rate64-light')]}
    study.prioritize_latency_points(plan)
    assert [r['kind'] for r in plan['order'][:6]] == [
        'full-2560', 'full-c128', 'ledger-c128', 'full-c64', 'ledger-c64', 'rate64-light']
    assert all(r['state'] == 'deferred' for r in plan['order'] if r['kind'].endswith('-4096'))
    assert len(plan['order']) == 9


def test_service_anchor_uses_successful_completions_not_steps():
    ref={'bins_120s':[{'agents':30,'start_s':0,'end_s':120}], 'successful_agents':30}
    assert study.measured_service_rate(ref)[0] == .25


def test_no_pool_keeps_checkpoint_but_removes_ram_tier():
    base={'environment':{'resources':{'warm_memory_capacity_mib':327680}},
          'mechanisms':{'shared_memory':{'borrow':'pressure'},'checkpoint':{'storage':'tiered','residency':'value'}},
          'execution':{}}
    changed=study.make_spec(base,'no-pool-2560')
    assert changed['mechanisms']['checkpoint']=={'storage':'disk','residency':'lru'}
    assert changed['environment']['resources']['warm_memory_capacity_mib']==0
    assert changed['execution']['concurrency_levels']==[2560]
    assert base['environment']['resources']['warm_memory_capacity_mib']==327680


def test_shared_ablation_requires_material_full_2560_use():
    empty={'peaks_bytes':{'shared_live_used_bytes':42*2**20}}
    assert study.shared_pool_trigger(empty)[0] is False
    borrowed={'peaks_bytes':{'shared_borrow_reserved_bytes':2**30}}
    assert study.shared_pool_trigger(borrowed)[:2] == (True, True)
    checkpointed={'peaks_bytes':{'warm_committed_bytes':2**30}}
    assert study.shared_pool_trigger(checkpointed)[:2] == (True, False)


def test_updated_scale_and_latency_specs_use_low_concurrency():
    base={'environment':{'resources':{}},'mechanisms':{},
          'execution':{'concurrency_levels':[1280],'arm_timeout_seconds':2400}}
    assert study.make_spec(base,'full-c128')['execution']['concurrency_levels']==[128]
    assert study.make_spec(base,'ledger-c64')['execution']['concurrency_levels']==[64]
    rate=study.make_spec(base,'rate64-light',3.0)
    assert rate['execution']['concurrency_levels']==[64]
    assert rate['execution']['poisson_arrival_window_fraction']==3.0


def test_4096_escalation_keeps_matching_ablation_scale_and_timeout():
    base={'environment':{'resources':{'warm_memory_capacity_mib':327680}},
          'mechanisms':{'shared_memory':{'borrow':'pressure'},
                        'checkpoint':{'storage':'tiered','residency':'value'}},
          'execution':{}}
    full = study.make_spec(base, 'full-4096')
    off = study.make_spec(base, 'no-pool-4096')
    low = study.make_spec(base, 'pool-low-4096', 64)
    for changed in (full, off, low):
        assert changed['execution']['concurrency_levels'] == [4096]
        assert changed['execution']['arm_timeout_seconds'] == 3600
    assert off['mechanisms']['checkpoint']['storage'] == 'disk'
    assert low['environment']['resources']['warm_memory_capacity_mib'] == 64*1024
    assert full['mechanisms'] == base['mechanisms']
