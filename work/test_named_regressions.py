"""Regression cases from the actual game OCR supplied by the user."""
import json,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from speaker_voices import SpeakerVoices,split_speaker,speaker_key
from subtitle_voice import SubtitleGate,SubtitleTracker
from subtitle_labels import separate_label,separate_captions
from openai_speech import OpenAISpeechWorker,added_speaker_name


class NamedRegressions(unittest.TestCase):
    def test_license_plate_beside_caption_is_not_spoken_as_extension(self):
        # Coordinates/text from background-live/frame-25.png.
        caption={'text':'Ширли: Она вчера съела резинку для волос...',
            'box':[400,628,882,650], 'words':[
                {'text':'Ширли:','coloured':True},
                {'text':'Она вчера съела резинку для волос...','coloured':False}]}
        plate={'text':'л- 84,','box':[335,629,373,646],'words':[]}
        continuation={'text':'И снова убежала.','box':[450,655,830,677],'words':[]}
        groups=separate_captions('',[plate,caption,continuation],('ширли',))
        self.assertEqual(groups,[['Ширли','Она вчера съела резинку для волос... И снова убежала.']])

    def accept(self,gate,text,start):
        result=None
        for offset in (0,.05,.1,.15,.2):
            speaker,body=split_speaker(text,gate.speaker_names)
            accepted=gate.update_dialogue(body,speaker,start+offset)
            if accepted:result=accepted
        return result

    def test_corrupted_names_all_resolve_to_one_persistent_character(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'voices.json'
            # This is the actual polluted registry, in its original order.
            entries=[('Джейсон','cedar'),('Лусия','marin'),('Брайан','ash'),
                     ("Бра'йаы",'coral'),("Бра'йан",'ballad'),('Эндрю','sage'),('Эудрю','echo'),
                     ('эадрюну','ballad'),('эурю','echo')]
            path.write_text(json.dumps({'speakers':[{'name':n,'voice':v} for n,v in entries]}),encoding='utf8')
            registry=SpeakerVoices(path)
            for name in ('Брайан','Брайаи','Брайай','БрайЛан',"Бра'йаы","Бра'йан"):
                self.assertEqual(registry.voice_for(name),'ash',name)
            for name in ('Джейсон','ДжёйсЪн','Джейеон'):
                self.assertEqual(registry.voice_for(name),'cedar',name)
            for name in ('Эндрю','Эудрю','эадрюну','эурю'):
                self.assertEqual(registry.voice_for(name),'sage',name)
            self.assertEqual(len(registry.entries),4)
            registry.save();restarted=SpeakerVoices(path)
            self.assertEqual(restarted.voice_for("Бра'йан"),'ash')
            self.assertEqual(restarted.voice_for('Эудрю'),'sage')

    def test_garbage_before_name_and_lost_colon_never_enter_dialogue(self):
        cases=[('ж х-- БрайЛан: Вытряси из них мои','Вытряси из них мои'),
               ("Бра'йаы: Вытряси из нйх мои деньгй! Боже.",'Вытряси из нйх мои деньгй! Боже.'),
               ('ДжёйсЪн: Пока, Брайан.','Пока, Брайан.'),
               ('Джейсон Привет, Эндрю.','Привет, Эндрю.')]
        for raw,expected in cases:
            self.assertEqual(split_speaker(raw)[1],expected)
        words=[{'text':'ж','coloured':False},{'text':'Бр?ан','coloured':True},
               {'text':'Вытряси','coloured':False},{'text':'деньги!','coloured':False}]
        label,body=separate_label('ж Бр?ан Вытряси деньги!',[{'text':'','words':words}])
        self.assertEqual(label,'Бр?ан');self.assertEqual(body,'Вытряси деньги!')

    def test_user_repeat_family_is_accepted_once_despite_voice_name_errors(self):
        gate=SubtitleGate(('брайан','эндрю'))
        family=[
            'Брайан: Вытряси из нйх мои деньги!. Боже.',
            "Бра'йаы: Вытряси из нйх мои деньгй! Боже.",
            "Бра'йан: Вытряси из нихемои",
            'Брайан: Вытрйси из них“ мои',
            'ж х-- БрайЛан: Вытряси из них мои']
        accepted=[self.accept(gate,line,index) for index,line in enumerate(family)]
        self.assertEqual(sum(value is not None for value in accepted),1)
        self.assertEqual(accepted[0].speaker,'брайан')
        self.assertNotIn('Брайан',accepted[0].text)
        family=['Эндрю: О, ага, ху-а! Я ждал твоего звонка.',
                'Эудрю: О, ага, ху-а! Я ждал твоего звонка.',
                'Эудрю: О, ага, «нале,тчикеа-ха, ху-а! Я ждал твоего звонка.',
                'Эндрю: О, ага, «налётчик»ДХа-ха, ху-а! Я ждал твоего звонка.']
        self.assertEqual(sum(self.accept(gate,line,20+index) is not None for index,line in enumerate(family)),1)

    def test_name_loss_and_partial_lower_line_do_not_switch_voice_or_repeat(self):
        gate=SubtitleGate(('брайан',))
        self.assertEqual(self.accept(gate,'Брайан: У них там лаба, и они задержали платеж',0).speaker,'брайан')
        continuation=self.accept(gate,'Кэл собиралея вчера вечедом забратьтплатеж, но недоехал.',2)
        self.assertEqual(continuation.speaker,'брайан')
        self.accept(gate,'Брайан: Донни“Й сейчас Неготов Обсуждать..',4)
        self.assertIsNone(self.accept(gate,'Брайан: Донни А сейчас Ае готовоб.ёуждат,ы:',5))
        self.assertIsNone(self.accept(gate,'Брайан: Донни я сейчас не готов',6))
        self.assertIsNone(self.accept(gate,'не готов обсуждать..',7))
        full='Джейсон: Ага, ага... ты что-то такое рассказывал... Прости, время поджинает. ЮхоЧу приехать и закупиться.'
        self.accept(gate,full,9)
        self.assertIsNone(self.accept(gate,'время поджимает. Я хочувприехать и закупиться. угу.',10))

    def test_added_label_in_model_transcript_is_rejected_but_real_address_kept(self):
        self.assertTrue(added_speaker_name('Джейсон: Привет!','Привет!','Джейсон'))
        self.assertTrue(added_speaker_name('Джейсон сказал: Привет!','Привет!','Джейсон'))
        self.assertFalse(added_speaker_name('Джейсон, стой!','Джейсон, стой!','Джейсон'))

    def test_simultaneous_dialogue_stays_separate_and_is_not_read_again(self):
        def row(text,y):return {'text':text,'box':[140,y,1100,y+34]}
        lines=[row('Джейсон: Ага, ага... ты что-то такое рассказывал... Прости,',513),
               row('время поджимает. Я:хочу приехать и закупиться.',563),
               row('Эндрю:Ха-ха, угу.',612)]
        groups=separate_captions('',lines,('джейсон','эндрю'))
        self.assertEqual(len(groups),2)
        self.assertIn('Я:хочу',groups[0][1])
        self.assertEqual(groups[1],['Эндрю','Ха-ха, угу.'])
        tracker=SubtitleTracker(('джейсон','эндрю'));accepted=[]
        for t in (0,.05,.1,.15,.2,.25,4,60,60.15):
            if t==60:tracker.update_frame([],59)
            accepted.extend(tracker.update_frame(groups,t))
        self.assertEqual([cue.speaker for cue in accepted],['джейсон','эндрю'])
        self.assertEqual(accepted[1].text,'Ха-ха, угу.')
        self.assertNotIn('Эндрю',accepted[1].text)

    def test_merged_colour_word_keeps_dialogue_after_colon(self):
        words=[{'text':'Эндрю:Ха-ха,','coloured':True},{'text':'угу.','coloured':False}]
        self.assertEqual(separate_label('',[{'text':'','words':words}]),('Эндрю','Ха-ха, угу.'))
        words=[{'text':'ДжейсонАга','coloured':True},{'text':'понятно.','coloured':False}]
        self.assertEqual(separate_label('',[{'text':'','words':words}]),('Джейсон','Ага понятно.'))
        words=[{'text':'ДжейсонПривет','coloured':True},{'text':'Эндрю!','coloured':False}]
        self.assertEqual(separate_label('',[{'text':'','words':words}]),('Джейсон','Привет Эндрю!'))

    def test_fused_name_cannot_create_another_voice(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'voices.json'
            path.write_text(json.dumps({'speakers':[{'name':'Джейсон','voice':'cedar'},
                                                    {'name':'джейсонага','voice':'coral'}]}),encoding='utf8')
            registry=SpeakerVoices(path)
            self.assertEqual(registry.voice_for('джейсонага'),'cedar')
            self.assertNotIn('джейсонага',registry.entries)

    def test_damaged_name_on_a_different_line_keeps_the_same_tracker_identity(self):
        tracker=SubtitleTracker(('брайан','эндрю'))
        accepted=[]
        for name,text,start in [('Эндрю','Я ждал твоего звонка.',0),
                                ('Эудрю','Пора выдвигаться к мосту.',2),
                                ("Бра'йаы",'Вытряси из них деньги!',4),
                                ('БрайЛан','Донни сейчас не готов обсуждать.',6)]:
            for offset in (0,.05,.1,.15,.2):
                accepted.extend(tracker.update_frame([(name,text)],start+offset))
        self.assertEqual([value.speaker for value in accepted],['эндрю','эндрю','брайан','брайан'])
        self.assertEqual(set(tracker.gates),{'эндрю','брайан'})

    def test_queue_has_no_label_and_dedup_is_independent_of_voice(self):
        class Client:
            key='fake'
            def abort(self):pass
            def close(self):pass
        # No background threads or API: inspect the actual submit boundary.
        with patch('openai_speech.SpeechClient',return_value=Client()),patch('openai_speech.threading.Thread'):
            worker=OpenAISpeechWorker(lambda *args:None);worker.set_enabled(True)
            try:
                worker.submit('ж х-- БрайЛан: Вытряси из них мои деньги!',0,'ash','Брайан')
                worker.submit('Вытряси из них мои деньги!',.3,'coral',"Бра'йаы")
                cue=worker.pending.get_nowait()
                self.assertEqual(cue.text,'Вытряси из них мои деньги')
                self.assertEqual(cue.voice,'ash')
                self.assertTrue(worker.pending.empty())
            finally:worker.close()


if __name__=='__main__':unittest.main()
