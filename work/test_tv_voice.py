import sys,time,threading,struct,unittest
from pathlib import Path
from unittest.mock import patch,Mock
BASE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(BASE/'outputs/GameVoice'))
from subtitle_voice import SubtitleGate
from speaker_voices import AcceptedSubtitle
from tv_voice import TVCaptions,TVSpeech,TV_VOICE,menu_visible,character_label
from subtitle_labels import separate_captions
from radio_mixer import AudioMixer

class FakeWorker:
    def __init__(self,emit,voice):
        self.emit=emit;self.voice=voice;self.calls=[];self.pending_count=0;self.model_id='eleven_v4_turbo'
        self.enabled=False;self.audio_mixer=None
    def set_enabled(self,value):self.enabled=value
    def set_model(self,model):self.model_id=model
    def submit(self,*args,**kwargs):self.calls.append(args);self.pending_count+=1
    def close(self):pass
class FakeMixer:
    output_device_name='station'
    busy=False
    def priority(self):return self.busy

class TVTests(unittest.TestCase):
    def test_observer_tv_does_not_archive_text_or_crops_and_menu_stays_silent(self):
        import subtitle_voice as game
        from PIL import Image
        for menu in ('Передача по телевизору','Карта Игра Настройки'):
            events=[];observer=game.Observer(lambda *e:events.append(e));observer.tv_enabled=True
            observer.capture=Mock();now=time.monotonic();image=Image.new('RGB',(1280,720))
            frames=iter([(i,now+i*.2,image) for i in range(1,4)])
            def next_frame(last):
                value=next(frames,None)
                if value is None:observer.stop_event.set()
                return value
            observer.capture.wait_for_frame.side_effect=next_frame
            async def recognize(frame,roi,**kwargs):
                return (menu if kwargs.get('unrestricted') else 'Только телевизионная реплика'),[],1
            ocr=Mock();ocr.recognize=recognize
            with patch.object(game,'RussianOCR',return_value=ocr),patch.object(game,'SubtitleSampleSink') as sink:
                observer.run();self.assertIsNone(observer.failure)
                for call in sink.return_value.observe.call_args_list:
                    self.assertEqual(call.args[4:7],('',[],[]));self.assertFalse(call.kwargs['save_image'])
            tv_events=[e for e in events if e[0]=='tv_subtitle']
            self.assertEqual(bool(tv_events),not menu_visible(menu))
            self.assertFalse(any(e[0]=='subtitle' for e in events))

    def test_tv_caption_gets_own_identity_and_dedup_without_name(self):
        tv=TVCaptions(SubtitleGate());groups=[('', 'Стрельбище «Гриффин». Когда есть ствол, на косметику можно забить!')]
        self.assertIsNone(tv.update(groups,(),1)[0])
        cue,blocked=tv.update(groups,(),1.2)
        self.assertFalse(blocked);self.assertEqual(cue.speaker,'телевизор')
        self.assertNotIn('Телевизор',cue.text)
        for stamp in (1.4,2,4):self.assertIsNone(tv.update(groups,(),stamp)[0])

    def test_names_and_menu_block_tv_and_do_not_inherit_previous_character(self):
        tv=TVCaptions(SubtitleGate())
        for name in ('Джейсон','Брет'):
            self.assertTrue(tv.update([(name,'Привет!'),('', 'Реклама по телевизору')],(name,),2)[1])
        self.assertTrue(tv.update([('', 'Телевизионная реплика')],(),2.5)[1])
        self.assertIsNone(tv.update([('', 'Телевизионная реплика')],(),4,True)[0])
        self.assertTrue(menu_visible('VI Карта Игра Статистика Настройки'))
        self.assertFalse(menu_visible('Эта игра стала настоящим открытием'))
        for text in ('Нажмите X, чтобы продолжить','Цена продажи 500 $','Радио'):
            for stamp in (5,5.3):self.assertIsNone(tv.update([('',text)],(),stamp)[0])

    def test_only_latest_pending_tv_line_and_no_archive_events(self):
        events=[];mixer=FakeMixer();tv=TVSpeech(FakeWorker,mixer,TV_VOICE,lambda *e:events.append(e))
        def tick(blocked=False,active=True):tv.tick(active,blocked,'station',1.25,True,'eleven_v4')
        tick();now=time.monotonic();tv.worker.pending_count=2
        tv.offer(AcceptedSubtitle('Старая строка','телевизор',now))
        tv.offer(AcceptedSubtitle('Новая строка','телевизор',now));tick()
        self.assertEqual(tv.worker.calls,[]);tv.worker.pending_count=1;tick()
        self.assertEqual(tv.worker.calls[0][:2],('Новая строка',now))
        self.assertEqual(tv.worker.calls[0][2],TV_VOICE)
        self.assertEqual(tv.worker.model_id,'eleven_v4')
        before=list(events)
        tv.event('voice_route',{'text':'Не сохранять'});tv.event('voice_audit',{'text':'Не сохранять'})
        self.assertEqual(events,before)
        tv.offer(AcceptedSubtitle('Остаток передачи','телевизор',now));mixer.busy=True;tick()
        self.assertFalse(tv.worker.enabled);self.assertIsNone(tv.latest)
        mixer.busy=False;tick();self.assertEqual(len(tv.worker.calls),1)
        self.assertEqual(tv.diagnostics()['counts']['cancelled_dialogue_audio'],2)

    def test_white_colons_and_character_mentions_cannot_cancel_tv_or_enter_main_queue(self):
        from subtitle_voice import SubtitleTracker
        known=('брайан','тема')  # Includes an old, polluted registry entry.
        for text in ('Тема: «Я видел воришку»','Брайан: Брэдли Г','Поверьте мне, это правда!'):
            line={'text':text,'box':[400,620,880,638],
                  'words':[{'text':word,'coloured':False} for word in text.split()]}
            groups=separate_captions(text,[line],known,label_policy=character_label)
            self.assertEqual(groups,[['',text]])
            main=SubtitleTracker(known,require_named=True);tv=TVCaptions(SubtitleGate())
            cues=[]
            for stamp in (1,1.2,1.4):
                named=[group for group in groups if group[0]]
                accepted=main.update_frame(named,stamp)
                self.assertFalse(accepted)
                cue,blocked=tv.update(groups,known,stamp,named_accepted=accepted)
                self.assertFalse(blocked)
                if cue:cues.append(cue)
            self.assertEqual([c.text for c in cues],[text])
            self.assertEqual(tv.diagnostics()['counts']['accepted'],1)

    def test_coloured_new_character_and_known_missing_colon_still_have_priority(self):
        for text,known in (('Новый: Я уже здесь.',()),('Брайан Я уже здесь.',('брайан',)),
                           ('Водитель: Выходите.',())):
            line={'text':text,'box':[400,620,880,638],
                  'words':[{'text':word,'coloured':index==0} for index,word in enumerate(text.split())]}
            groups=separate_captions(text,[line],known,label_policy=character_label)
            self.assertTrue(groups[0][0],text)
            self.assertTrue(TVCaptions(SubtitleGate()).update(groups,known,1)[1])

    def test_pause_reason_is_visible_even_when_tv_starts_on_menu(self):
        events=[];tv=TVSpeech(FakeWorker,FakeMixer(),TV_VOICE,lambda *e:events.append(e))
        tv.tick(True,True,'station',1.25,True,reason='menu')
        self.assertIn('меню',events[-1][0])
        self.assertEqual(tv.diagnostics()['block_reason'],'menu')
        self.assertNotIn('text',str(tv.diagnostics()))

    def test_real_worker_prefetches_next_tv_phrase_and_preserves_white_colon(self):
        from openai_speech import OpenAISpeechWorker
        texts=[];first_playing=threading.Event();next_ready=threading.Event();release=threading.Event()
        class Client:
            key='fake';first_chunk_ms=1;ws=None;needs_reconnect=False
            def maintain(self):pass
            def abort(self):pass
            def close(self):pass
            def synthesize(self,text,number):
                texts.append(text)
                if number==2:next_ready.set()
                return b'\0\0'*480,''
        class Mixer(FakeMixer):
            def play_character(self,pcm,volume,valid,started,background=False):
                started();first_playing.set()
                release.wait(2)
                return valid()
        def factory(emit,voice):return OpenAISpeechWorker(emit,voice,client_factory=lambda *a:Client(),key='fake')
        tv=TVSpeech(factory,Mixer(),TV_VOICE,lambda *e:None)
        with patch('openai_speech.change_tempo',side_effect=lambda pcm,factor:pcm),patch('openai_speech.output_index',return_value=0):
            try:
                tv.tick(True,False,'station',1.25,True)
                tv.offer(AcceptedSubtitle('Тема: Происшествие в городе','телевизор',time.monotonic()))
                tv.tick(True,False,'station',1.25,True)
                self.assertTrue(first_playing.wait(1))
                tv.offer(AcceptedSubtitle('Следующая телевизионная фраза','телевизор',time.monotonic()))
                tv.tick(True,False,'station',1.25,True)
                self.assertTrue(next_ready.wait(1),'Next request must start before current playback ends')
                self.assertEqual(texts,['Тема: Происшествие в городе','Следующая телевизионная фраза'])
                self.assertLessEqual(tv.worker.pending_count,2)
            finally:release.set();tv.close()

    def test_tv_failure_requires_toggle_and_does_not_stop_main_voice(self):
        mixer=FakeMixer();tv=TVSpeech(FakeWorker,mixer,TV_VOICE,lambda *e:None)
        def tick(active=True):tv.tick(active,False,'station',1.25,True)
        tick();tv.event('error','Denied');tick()
        self.assertFalse(tv.worker.enabled);tick();self.assertFalse(tv.worker.enabled)
        tick(False);tick(True);self.assertTrue(tv.worker.enabled)

    def test_dialogue_takes_over_background_without_overlap_or_clearing_it(self):
        mixer=AudioMixer();mixer.ensure_stream=lambda:None;results=[]
        tv=threading.Thread(target=lambda:results.append(mixer.play_character(
            struct.pack('<h',1000)*48000,1,lambda:not mixer.priority(),lambda:None,background=True)),daemon=True)
        tv.start();deadline=time.monotonic()+1
        while mixer.character is None and time.monotonic()<deadline:time.sleep(.001)
        first=bytearray(1920);mixer.callback(first,480,None,None)
        self.assertEqual(struct.unpack('<h',first[:2])[0],1000)
        mixer.set_pending(1)
        main=threading.Thread(target=lambda:mixer.play_character(struct.pack('<h',2000)*480,1,lambda:True,lambda:None),daemon=True)
        main.start();deadline=time.monotonic()+1
        while mixer.character and struct.unpack('<h',mixer.character['data'][:2])[0]!=2000 and time.monotonic()<deadline:time.sleep(.001)
        tv.join(1);self.assertFalse(tv.is_alive());self.assertEqual(results,[False])
        out=bytearray(1920);mixer.callback(out,480,None,None);main.join(1)
        self.assertEqual(struct.unpack('<h',out[:2])[0],2000);self.assertFalse(main.is_alive())
        mixer.close()

if __name__=='__main__':unittest.main()
