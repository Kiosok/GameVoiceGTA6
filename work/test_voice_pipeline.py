import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from voice_pipeline import Bank,InstanceLock,atomic_json,closed_parts,load,recording_identity
from pipeline_install import make_reference,publish,NeedsAttention
from speaker_voices import SpeakerVoices

ID='a'*20


def clip(root,name='Джейсон',seconds=30,start=0,tag='one'):
    path=Path(root)/(tag+'.wav')
    # Different deterministic signals to exercise PCM dedup, not silent test data.
    pcm=(123+(len(tag)*93)).to_bytes(2,'little')*round(seconds*24000)
    with wave.open(str(path),'wb') as wav:
        wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(pcm)
    return {'id':tag,'character':name,'session':'source','start':start,'end':start+seconds,
        'text_key':tag,'pcm_hash':hashlib.sha256(path.read_bytes()).hexdigest(),'seconds':seconds,'path':str(path),
        'evidence':{'clean_word_intervals':[[a,min(a+12000,round(seconds*24000))]
            for a in range(0,round(seconds*24000),12000)]}}


class FakeAPI:
    def __init__(self,existing=True,fail_probe=False,lose_response=False):
        self.voice_data={'voice_id':ID,'name':'GameVoice — Джейсон','category':'cloned',
                         'samples':[{'sample_id':'old','file_name':'old.wav','duration_secs':12}]}
        self.existing=existing;self.fail_probe=fail_probe;self.lose_response=lose_response
        self.calls=[];self.adds=0

    def request(self,method,path,body=None,content_type=None):
        self.calls.append((method,path))
        if method=='GET' and path=='/v1/voices':return {'voices':[self.voice_data] if self.existing else []}
        if path=='/v1/user/subscription':return {'voice_limit':30,'voice_slots_used':14,'can_use_instant_voice_cloning':True}
        if method=='DELETE':
            self.voice_data['samples']=[s for s in self.voice_data['samples'] if s['sample_id']!=path.rsplit('/',1)[-1]]
            return {'status':'ok'}
        raise AssertionError((method,path))

    def voice(self,identifier):return self.voice_data

    def add(self,record,raw):
        self.adds+=1
        self.voice_data['samples'].append({'sample_id':'new' if self.adds==1 else 'new'+str(self.adds),'file_name':Path(record['filename']).stem+'.mp3','duration_secs':record['wave_seconds']})
        self.voice_data['description']=record['marker'];self.existing=True
        if self.lose_response:self.lose_response=False;raise TimeoutError('lost response after commit')
        return {'status':'ok','voice_id':ID,'requires_verification':False}

    def probe(self,identifier,target):
        if self.fail_probe:raise RuntimeError('TTS unavailable')
        Path(target).write_bytes(b'valid-in-fake')


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.bank=Bank(self.root)
    def tearDown(self):self.bank.close();self.temp.cleanup()
    def rows(self):
        self.bank.add(clip(self.root,tag='alpha'))
        self.bank.add(clip(self.root,start=40,tag='second'))
        return self.bank.rows('Джейсон')

    def test_storage_move_preserves_session_identity_and_job_dedup(self):
        original=str(self.root/'original-location')
        moved=self.root/'new-location';moved.mkdir()
        atomic_json(moved/'session.json',{'session':original,'sample_rate':48000,
                                        'continuous':True,'estimated_first_sample_monotonic':1})
        atomic_json(moved/'status.json',{'state':'finished','seconds':10})
        (moved/'audio-timeline.jsonl').write_text(json.dumps({'type':'segment','file':'a.wav','sample_offset':0})+'\n')
        before={'session':original,'start':0,'end':480000,'file':'a.wav','rate':48000}
        identifier=self.bank.enqueue(before)
        self.assertEqual(recording_identity(moved),original)
        after=closed_parts(moved)[0]
        self.assertEqual(self.bank.enqueue(after),identifier)
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)

    def test_open_part_never_processed_rollover_is_proof(self):
        atomic_json(self.root/'session.json',{'continuous':True,'sample_rate':48000,'estimated_first_sample_monotonic':1})
        atomic_json(self.root/'status.json',{'state':'recording','seconds':2701})
        timeline=self.root/'audio-timeline.jsonl'
        timeline.write_text(json.dumps({'type':'segment','file':'a.wav','sample_offset':0})+'\n')
        self.assertEqual(closed_parts(self.root),[])
        with timeline.open('a') as output:
            output.write(json.dumps({'type':'segment','file':'b.wav','sample_offset':2700*48000})+'\n{incomplete')
        result=closed_parts(self.root)
        self.assertEqual(len(result),1);self.assertEqual(result[0]['end'],2700*48000)

    def test_restarts_overlap_text_and_pcm_do_not_inflate_minute(self):
        self.assertTrue(self.bank.add(clip(self.root,tag='alpha')))
        self.assertFalse(self.bank.add(clip(self.root,start=15,tag='overlap')))
        self.assertFalse(self.bank.add(clip(self.root,start=40,tag='alpha')))
        self.bank.close();self.bank=Bank(self.root)
        self.assertEqual(self.bank.counters()[0]['seconds'],30)

    def test_threshold_before_any_network_and_padding_does_not_count(self):
        item=clip(self.root,seconds=80,tag='long')
        item['evidence']['clean_word_intervals']=[[0,round(59.9*24000)]];item['seconds']=59.9
        self.bank.add(item);api=FakeAPI()
        with self.assertRaises(ValueError):publish('Джейсон',self.bank.rows('Джейсон'),None,self.root/'out',self.root/'overlay.json',api)
        self.assertEqual(api.calls,[]);self.assertEqual(api.adds,0)

    def test_ready_update_same_id_one_post_then_old_sample_removal(self):
        api=FakeAPI();rows=self.rows();overlay=self.root/'overlay.json'
        result=publish('Джейсон',rows,{'voice':ID,'manual':True},self.root/'out',overlay,api)
        self.assertEqual(result['state'],'complete');self.assertEqual(result['voice_id'],ID)
        self.assertGreaterEqual(result['clean_speech_seconds'],60)
        self.assertEqual([s['sample_id'] for s in api.voice_data['samples']],['new'])
        clone_journal=self.root/'clones'/(hashlib.sha256('Джейсон'.encode()).hexdigest()[:16]+'.json')
        self.assertEqual(load(clone_journal)['voice_id'],ID)
        self.assertEqual(load(clone_journal)['sample_ids'],['new'])
        publish('Джейсон',rows,{'voice':ID,'manual':True},self.root/'out',overlay,api)
        self.assertEqual(api.adds,1)
        overlay.unlink()
        publish('Джейсон',rows,{'voice':ID,'manual':True},self.root/'out',overlay,api)
        self.assertEqual(load(overlay)['characters'][0]['voice_id'],ID)
        self.assertEqual(api.adds,1)

    def test_candidate_name_requires_repeated_dialogue_and_rejects_roles(self):
        from pipeline_jobs import known_names
        rows=[]
        for name in ('Эндрю','Менеджер','КсдЫы'):
            for index in range(9):
                text=('Привет, я здесь.','Как у тебя дела?','Поехали к мосту.')[index%3]
                rows.append({'groups':[[name,text]],'accepted':[{'speaker':name,'text':text}]})
        names=known_names(rows)
        self.assertEqual(names.get('эндрю'),'Эндрю')
        self.assertNotIn('менеджер',names);self.assertNotIn('ксдыы',names)

    def test_lost_update_response_reconciles_without_repeat(self):
        api=FakeAPI(lose_response=True);rows=self.rows()
        with self.assertRaises(TimeoutError):publish('Джейсон',rows,{'voice':ID,'manual':True},self.root/'out',self.root/'overlay.json',api)
        result=publish('Джейсон',rows,{'voice':ID,'manual':True},self.root/'out',self.root/'overlay.json',api)
        self.assertEqual(result['state'],'complete');self.assertEqual(api.adds,1)

    def test_lost_create_response_reconciles_unique_marker(self):
        api=FakeAPI(existing=False,lose_response=True);rows=self.rows()
        with self.assertRaises(TimeoutError):publish('Джейсон',rows,None,self.root/'out',self.root/'overlay.json',api)
        result=publish('Джейсон',rows,None,self.root/'out',self.root/'overlay.json',api)
        self.assertEqual(result['voice_id'],ID);self.assertEqual(api.adds,1)

    def test_probe_failure_preserves_old_voice_and_no_publication(self):
        api=FakeAPI(fail_probe=True)
        with self.assertRaises(RuntimeError):publish('Джейсон',self.rows(),{'voice':ID,'manual':True},self.root/'out',self.root/'overlay.json',api)
        self.assertIn('old',[s['sample_id'] for s in api.voice_data['samples']])
        self.assertFalse((self.root/'overlay.json').exists())

    def test_hot_reload_does_not_overwrite_manual_settings(self):
        registry=SpeakerVoices(self.root/'manual.json',voices=(ID,'b'*20),seeds={},allow_voice_ids=True)
        registry.assign('Джейсон',ID);registry.speed=1.35;registry.save()
        before=registry.path.read_bytes();overlay=self.root/'overlay.json'
        atomic_json(overlay,{'revision':'1','characters':[{'name':'Джейсон','voice_id':'c'*20,'expected_voice':ID,'state':'ready','clean_speech_seconds':60}]})
        self.assertTrue(registry.reload_automatic(overlay));self.assertEqual(registry.voice_for('Джейсон'),'c'*20)
        self.assertEqual(registry.path.read_bytes(),before)
        registry.assign('Джейсон','b'*20)
        self.assertEqual(registry.voice_for('Джейсон'),'b'*20)
        self.assertEqual(registry.speed,1.35)

    def test_bad_overlay_keeps_last_verified_bindings(self):
        registry=SpeakerVoices(self.root/'manual.json',voices=(ID,),seeds={},allow_voice_ids=True)
        overlay=self.root/'overlay.json'
        atomic_json(overlay,{'revision':'1','characters':[{'name':'Джейсон','voice_id':ID,'state':'ready','clean_speech_seconds':60}]})
        self.assertTrue(registry.reload_automatic(overlay));overlay.write_text('{partial')
        self.assertFalse(registry.reload_automatic(overlay));self.assertEqual(registry.voice_for('Джейсон'),ID)

    def test_queue_durable_and_idempotent(self):
        part={'session':'source','start':0,'end':2700,'file':'part.wav'}
        self.assertEqual(self.bank.enqueue(part),self.bank.enqueue(part))
        self.assertEqual(self.bank.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)

    def test_singleton_os_lock(self):
        with InstanceLock(self.root/'lock'):
            with self.assertRaises(RuntimeError):
                with InstanceLock(self.root/'lock'):pass

    def test_windows_reader_lock_cannot_terminate_status_writer(self):
        import os
        real_replace=os.replace
        attempts=[]
        def busy_then_replace(source,target):
            attempts.append(1)
            if len(attempts)<4:raise PermissionError('transient reader handle')
            return real_replace(source,target)
        with patch('voice_pipeline.os.replace',side_effect=busy_then_replace):
            atomic_json(self.root/'status.json',{'seconds':2701})
        self.assertEqual(load(self.root/'status.json')['seconds'],2701)
        self.assertEqual(len(attempts),4)

    def test_second_threshold_blocks_before_network(self):
        rows=self.rows();api=FakeAPI()
        with self.assertRaises(ValueError):
            publish('Джейсон',rows,{'voice':ID,'manual':True},self.root/'out',self.root/'overlay.json',api,milestone=160)
        self.assertEqual(api.adds,0);self.assertEqual(api.calls,[])

    def test_two_stages_preserve_id_and_retry_does_not_repeat(self):
        for index in range(4):
            self.bank.add(clip(self.root,seconds=40,start=index*50,tag='x'*(index+1)))
        rows=self.bank.rows('Джейсон');api=FakeAPI();out=self.root/'out';overlay=self.root/'overlay.json'
        first=publish('Джейсон',rows,{'voice':ID,'manual':True},out,overlay,api)
        second=publish('Джейсон',rows,{'voice':ID,'manual':True},out,overlay,api,milestone=160)
        self.assertEqual(first['voice_id'],second['voice_id']);self.assertEqual(api.adds,2)
        self.assertLessEqual(second['wave_seconds'],120)
        self.assertEqual(load(overlay)['characters'][0]['installed_milestone'],160)
        self.assertEqual([s['sample_id'] for s in api.voice_data['samples']],['new2'])
        publish('Джейсон',rows,{'voice':ID,'manual':True},out,overlay,api,milestone=160)
        self.assertEqual(api.adds,2)
        self.assertNotEqual(first['sha256'],second['sha256'])

    def test_improved_reference_prefers_confirmed_speaker_evidence(self):
        from pipeline_install import make_improved_reference
        rows=[]
        for index in range(5):
            item=clip(self.root,seconds=40,start=index*50,tag='a'*(index+1))
            item['evidence'].update(anchored=index>=2,word_share=.99)
            item['evidence']=json.dumps(item['evidence']);rows.append(item)
        reference=make_improved_reference(rows,self.root/'better.wav')
        self.assertEqual(set(reference['sources']),{r['id'] for r in rows[2:]})
        self.assertLessEqual(reference['wave_seconds'],120)


if __name__=='__main__':unittest.main()
