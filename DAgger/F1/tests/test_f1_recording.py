import json
import os
from pathlib import Path
import numpy as np
import pytest
from f1dagger.contract import CONTRACT
from f1dagger.recording import F1Recorder,make_sink,audit
from f1dagger.export import select_segments,export_dataset,_resample_groups
from test_f1_policy import observation


def raw_episode(tmp_path,sources=None,*,fps=10,synthetic=False,control_offset_ns=-1,epochs=None,interventions=None,timestamps_ns=None):
    run=tmp_path/'raw';run.mkdir()
    sources=sources or ['human_arm','human_hold','home','human_hand','human_hold']
    recorder=F1Recorder(run/'journal',episode_id='test-f1',task_prompt='pick',
        binding={'execution_mode':'powered','recording_mode':'hil-record','synthetic':synthetic})
    sink=make_sink(run/'payload','hil-record')
    for i,source in enumerate(sources):
        offset = round(i*1_000_000_000/fps) if timestamps_ns is None else timestamps_ns[i]
        obs=observation(i,1_000_000_000+offset)
        sink.submit(obs)
        epoch = 2 if epochs is None else epochs[i]
        intervention = 1 if interventions is None else interventions[i]
        recorder.record_timestamped_observation(obs,hil_state='hil_latched',intervention_id=intervention,policy_epoch=epoch)
        command=np.zeros(16);command[0]=i+1
        t=obs.capture_started_monotonic_ns+control_offset_ns
        if i == 0:
            grip_stamp=t-1
        pubs=[{'topic_key':'joint_command','monotonic_ns':t-1,'publisher_gid':'aa'*16}]
        if i == 0:
            pubs += [{'topic_key':key,'monotonic_ns':grip_stamp,'publisher_gid':'aa'*16} for key in ('left_gripper_command','right_gripper_command')]
        recorder.control(now=t,state='hil_latched',intervention=intervention,epoch=epoch,
            selected=command,submitted=command,source=source,measured=np.zeros(16),sequence=i+1,
            publication_complete=True,execution_mode='powered',action_source_timestamps_ns=[t-1]*14+[grip_stamp]*2,
            publications=pubs)
    assert sink.close()
    assert recorder.close()
    report=audit(run);assert report['valid'],report
    review=tmp_path/'review.json'
    review.write_text(json.dumps(dict(contract=CONTRACT,manifest_sha256=report['manifest_sha256'],
                                     reviewer='offline-test-fixture',accepted_interventions=sorted(set(interventions or [1])))))
    return run,review


def test_hil_home_boundaries_and_true_action_labels(tmp_path):
    run,review=raw_episode(tmp_path)
    segments,report,manifest=select_segments(run,json.loads(review.read_text()),fps=10)
    assert [len(s) for s in segments]==[2,2]
    assert [r['action'][0] for s in segments for r in s]==[1,2,4,5]
    assert all(r['state'][0]==0 for s in segments for r in s)
    assert manifest['schema_version']==3 and manifest['action_contract']['dimension']==16
    assert report['training_ready'] is False


def test_raw_hash_and_review_identity_are_mandatory(tmp_path):
    run,review=raw_episode(tmp_path)
    changed=json.loads(review.read_text());changed['manifest_sha256']='bad'
    with pytest.raises(ValueError,match='another'):select_segments(run,changed)
    with (run/'journal/control.jsonl').open('a') as stream:stream.write('{}\n')
    assert not audit(run)['valid']


def test_incomplete_raw_never_exports(tmp_path):
    run=tmp_path/'broken';run.mkdir()
    assert not audit(run)['valid']


class FakeWriter:
    instances=[]
    @classmethod
    def create(cls,**kwargs):
        self=cls();self.path=kwargs['root'];self.path.mkdir();self.frames=[];self.episodes=[];self.fps=kwargs['fps']
        cls.instances.append(self);return self
    def add_frame(self,frame):self.frames.append(frame)
    def save_episode(self):self.episodes.append(len(self.frames))
    def finalize(self):pass


def test_export_transaction_and_provenance(tmp_path):
    run,review=raw_episode(tmp_path)
    out=tmp_path/'export'
    report=export_dataset(run,review,out,None,fps=10,writer_class=FakeWriter)
    assert out.is_dir() and not out.with_name('export.partial').exists()
    assert report['selected_segments']==2 and not report['training_ready']
    rows=[json.loads(line) for line in (out/'f1_source.jsonl').read_text().splitlines()]
    assert [r['observation_id'] for r in rows]==[0,1,3,4]
    assert [r['episode_index'] for r in rows]==[0,0,1,1]
    with pytest.raises(FileExistsError):export_dataset(run,review,out,None,writer_class=FakeWriter)


def test_failed_export_stays_partial(tmp_path):
    run,review=raw_episode(tmp_path)
    class Broken(FakeWriter):
        def add_frame(self,frame):raise OSError('disk failure')
    with pytest.raises(OSError):export_dataset(run,review,tmp_path/'out',None,fps=10,writer_class=Broken)
    assert not (tmp_path/'out').exists() and (tmp_path/'out.partial').is_dir()


def test_default_30hz_contract_preserves_real_observations_and_publications(tmp_path):
    config=json.loads((Path(__file__).parents[1]/'configs/f1_09_hil.json').read_text())
    assert config['capture_hz']==config['policy']['frequency_hz']==30
    run,review=raw_episode(tmp_path,['human_arm']*31,fps=30)
    out=tmp_path/'export30'
    report=export_dataset(run,review,out,None,writer_class=FakeWriter)
    writer=FakeWriter.instances[-1]
    assert report['fps']==writer.fps==30
    assert report['selected_frames']==31 and report['selected_segments']==1
    rows=[json.loads(line) for line in (out/'f1_source.jsonl').read_text().splitlines()]
    assert [r['observation_id'] for r in rows]==list(range(31))
    assert rows[-1]['reference_monotonic_ns']-rows[0]['reference_monotonic_ns']==1_000_000_000
    assert [float(frame['action'][0]) for frame in writer.frames]==list(range(1,32))
    assert all(np.array_equal(frame['observation.state'],np.zeros(16)) for frame in writer.frames)


def test_default_export_does_not_duplicate_10hz_frames_to_claim_30hz(tmp_path):
    run,review=raw_episode(tmp_path,['human_arm']*8,fps=10)
    with pytest.raises(ValueError,match='no reviewed, contiguous'):
        select_segments(run,json.loads(review.read_text()))
    segments,report,_=select_segments(run,json.loads(review.read_text()),fps=10)
    assert report['fps']==10 and [len(s) for s in segments]==[8]


@pytest.mark.skipif(not os.environ.get('F1_TEST_LEROBOT_ROOT'),reason='requires the pinned LeRobot dataset environment')
@pytest.mark.parametrize('fps,synthetic',[(10,False),(30,False),(30,True)])
def test_real_lerobot_v3_video_and_reader_roundtrip(tmp_path,fps,synthetic):
    run,review=raw_episode(tmp_path,fps=fps,synthetic=synthetic)
    options={} if fps==30 else {'fps':10}
    report=export_dataset(run,review,tmp_path/'real',os.environ['F1_TEST_LEROBOT_ROOT'],**options)
    info=json.loads((tmp_path/'real/meta/info.json').read_text())
    assert report['reader_roundtrip_verified']
    assert report['training_ready'] is (not synthetic)
    assert report['human_review_accepted'] is (not synthetic)
    assert report['synthetic'] is synthetic
    assert report['fps']==info['fps']==fps


def test_first_control_cannot_retroactively_label_pre_start_camera_frame(tmp_path):
    run, review = raw_episode(tmp_path, ['human_arm']*3, control_offset_ns=9)
    segments, report, _ = select_segments(run, json.loads(review.read_text()), fps=10)
    assert [[row['observation_id'] for row in segment] for segment in segments] == [[1, 2]]
    assert report['rejected_observations'] == 1


@pytest.mark.parametrize('boundary', ['epoch', 'intervention'])
def test_frame_interval_crossing_control_boundary_is_excluded(tmp_path, boundary):
    change = {'epochs': [2, 3, 3, 3]} if boundary == 'epoch' else {'interventions': [1, 2, 2, 2]}
    run, review = raw_episode(tmp_path, ['human_arm']*4, control_offset_ns=9, **change)
    segments, report, _ = select_segments(run, json.loads(review.read_text()), fps=10)
    assert [[row['observation_id'] for row in segment] for segment in segments] == [[2, 3]]
    assert report['rejected_observations'] == 2


def test_slightly_slow_capture_keeps_time_axis_without_fragmentation(tmp_path):
    offsets = [i*34_100_000 for i in range(90)]
    run, review = raw_episode(tmp_path, ['human_arm']*len(offsets), timestamps_ns=offsets)
    groups, report, _ = select_segments(run, json.loads(review.read_text()))
    assert len(groups) == report['source_contiguous_groups'] == 1
    rows = groups[0]
    origin = 1_000_000_010
    assert len(rows) == 92
    assert [r['reference_monotonic_ns']-origin for r in rows] == [round(i*1e9/30) for i in range(92)]
    assert rows[-1]['reference_monotonic_ns'] <= origin+offsets[-1]
    assert report['held_observation_samples'] > 0
    assert report['unique_source_observations'] < report['selected_frames']
    for row in rows:
        assert row['source_observation_monotonic_ns'] == origin+offsets[row['observation_id']]
        assert 0 <= row['observation_age_ns'] < 34_100_000
        assert row['command_monotonic_ns'] <= row['reference_monotonic_ns']


def test_jitter_uses_past_observation_and_current_published_action(tmp_path):
    offsets = [i*40_000_000 for i in range(8)]
    run, review = raw_episode(tmp_path, ['human_arm']*8, timestamps_ns=offsets,
                             control_offset_ns=-10_000_000)
    groups, report, _ = select_segments(run, json.loads(review.read_text()))
    assert len(groups) == 1 and report['held_observation_samples'] > 0
    # At 33.3 ms, observation 1 (40 ms) is in the future, but command 2
    # was already published at 30 ms. Repeating action 1 would be wrong.
    row = groups[0][1]
    assert row['observation_id'] == 0 and row['observation_reused']
    assert row['state'][0] == 0
    assert row['action'][0] == 2 and row['command_sequence'] == 2
    assert row['command_monotonic_ns'] == 1_030_000_000


def test_grid_never_extrapolates_past_last_observation(tmp_path):
    run, review = raw_episode(tmp_path, ['human_arm']*3,
                             timestamps_ns=[0, 33_333_333, 66_666_666])
    groups, _, _ = select_segments(run, json.loads(review.read_text()))
    # The third 30 Hz grid point is 1 ns beyond available evidence.
    assert [r['observation_id'] for r in groups[0]] == [0, 1]


def test_real_gap_is_not_filled_or_time_compressed(tmp_path):
    offsets = [0, 33_333_333, 66_666_667, 266_666_667, 300_000_000, 333_333_334]
    run, review = raw_episode(tmp_path, ['human_arm']*6, timestamps_ns=offsets)
    groups, report, _ = select_segments(run, json.loads(review.read_text()))
    assert len(groups) == report['source_contiguous_groups'] == 2
    assert [r['observation_id'] for r in groups[0]] == [0, 1, 2]
    assert [r['observation_id'] for r in groups[1]] == [3, 4, 5]
    assert groups[1][0]['reference_monotonic_ns']-groups[0][-1]['reference_monotonic_ns'] == 200_000_000


@pytest.mark.parametrize('boundary', ['home', 'epoch', 'intervention'])
def test_held_observations_do_not_bridge_control_boundaries(tmp_path, boundary):
    sources = ['human_arm']*90
    options = {}
    if boundary == 'home':
        sources[45] = 'home'
    elif boundary == 'epoch':
        options['epochs'] = [2]*45+[3]*45
    else:
        options['interventions'] = [1]*45+[2]*45
    run, review = raw_episode(tmp_path, sources,
                             timestamps_ns=[i*34_100_000 for i in range(90)], **options)
    groups, _, _ = select_segments(run, json.loads(review.read_text()))
    assert len(groups) == 2
    assert all(r['observation_id'] < 45 for r in groups[0])
    assert all(r['observation_id'] >= (46 if boundary == 'home' else 45) for r in groups[1])
    assert all(len({(r['policy_epoch'], r['intervention_id']) for r in g}) == 1 for g in groups)


@pytest.mark.parametrize('expired', ['state', 'camera', 'control', 'epoch'])
def test_resampling_rechecks_freshness_and_control_at_grid_time(expired):
    # A source observation is valid at t=0, but cannot necessarily be held
    # until the next grid point. A rejected grid must remain a real gap.
    origin=1_000_000_000
    group=[]
    for i,offset in enumerate((0,40_000_000,80_000_000,120_000_000)):
        group.append(dict(observation_id=i,reference_monotonic_ns=origin+offset,
            state=[0.]*16,state_timestamps_ns=[origin+offset]*16,
            frames=[dict(host_received_monotonic_ns=origin+offset)],
            intervention_id=1,policy_epoch=2))
    if expired=='state':group[0]['state_timestamps_ns']=[origin-240_000_000]*16
    if expired=='camera':group[0]['frames'][0]['host_received_monotonic_ns']=origin-190_000_000
    controls=[dict(monotonic_ns=origin+t,command_sequence=i,stream_sequence=i,
                   submitted=[float(i)]*16,action_source_timestamps_ns=[origin+t]*16,epoch=2)
              for i,t in enumerate((-20_000_000,40_000_000,80_000_000,120_000_000))]
    if expired=='epoch':controls.insert(1,dict(controls[0],monotonic_ns=origin+20_000_000,epoch=3))
    groups,rejected=_resample_groups([group],controls,[c['monotonic_ns'] for c in controls],30,
                                     lambda c,intervention,epoch:c['epoch']==epoch)
    assert rejected==1
    assert groups[0][0]['reference_monotonic_ns']==origin+66_666_667
    assert len(groups[0])==2
