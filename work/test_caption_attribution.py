"""Regression coverage for the actual wrong-speaker, HUD and spoken-name audit."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from subtitle_labels import separate_captions
from subtitle_voice import SubtitleTracker
from speaker_voices import SpeakerVoices, split_speaker, resolve_speaker_key
from elevenlabs_speech import registry


class AttributionTests(unittest.TestCase):
    def accept(self, tracker, groups, start=0):
        cues=[]
        for offset in (0,.05,.1,.15,.2):cues.extend(tracker.update_frame(groups,start+offset))
        return cues

    def test_lowercase_role_words_do_not_need_prior_registry_entry(self):
        self.assertEqual(split_speaker('Местные жители: Не бойся.',()),
                         ('Местные жители','Не бойся.'))
        tracker=SubtitleTracker(require_named=True)
        self.accept(tracker,[['Лусия','Да, я поняла.']])
        groups=separate_captions('Местные жители: Не бойся.',[],())
        cues=self.accept(tracker,groups,1)
        self.assertEqual([(c.speaker,c.text) for c in cues],[('местные жители','Не бойся.')])

    def test_three_rows_fit_and_multiple_speakers_keep_their_own_lines(self):
        from device_settings import ROI_DEFAULTS
        roi=ROI_DEFAULTS['small']
        def line(text,left,top,right,bottom):
            self.assertLessEqual(roi[1]*720,top)
            self.assertGreaterEqual(roi[3]*720,bottom)
            return {'text':text,'box':[left,top,right,bottom],'words':[]}
        groups=separate_captions('',[
            line('Лусия: Я уже здесь.',440,582,840,598),
            line('Кайл: Тогда идём',450,608,830,624),
            line('к выходу.',565,634,715,650)],('лусия','кайл'))
        self.assertEqual(groups,[['Лусия','Я уже здесь.'],['Кайл','Тогда идём к выходу.']])

    def test_split_label_and_body_same_baseline_are_rejoined_before_continuation(self):
        lines=[{'text':'Не бойся,','box':[530,612,640,628],'words':[
                    {'text':'Не бойся,','coloured':False}]},
               {'text':'Местные жители:','box':[400,615,525,625],'words':[
                    {'text':'Местные','coloured':True},{'text':'жители:','coloured':True}]},
               {'text':'мы рядом.','box':[570,635,700,650],'words':[]}]
        groups=separate_captions('',lines,())
        self.assertEqual(groups,[['Местные жители','Не бойся, мы рядом.']])
        tracker=SubtitleTracker(require_named=True)
        self.accept(tracker,[['Лусия','Сейчас придём.']])
        self.assertEqual(self.accept(tracker,groups,1)[0].speaker,'местные жители')

    def test_hud_column_is_not_appended_to_real_two_line_dialogue(self):
        def line(text,box):return {'text':text,'box':box,'words':[]}
        groups=separate_captions('',[
            line('Лусия: Измени номера',[410,612,870,628]),
            line('BRAVADO',[978,613,1028,626]),
            line('и зарегистрируй машину.',[470,634,810,650]),
            line('BISON ЛУСИИ',[978,632,1053,645])],('лусия',))
        self.assertEqual(groups,[['Лусия','Измени номера и зарегистрируй машину.']])

    def test_new_unlabelled_menu_text_never_borrows_previous_clone(self):
        tracker=SubtitleTracker(require_named=True)
        self.accept(tracker,[['Лусия','Теперь пойдём домой.']])
        for start,text in enumerate(('ЦЕНА ПРОДАЖ','ТРЕКЕР','BRAVADO ввон','тюнинг',
                                     'Река Эмеральд','ПОТЕРЯННЫЙ РАЙ'),1):
            self.assertEqual(self.accept(tracker,[['',text]],start),[])
        self.assertEqual(self.accept(tracker,[['е','ГЛАВА 2']],9),[])

    def test_unlabelled_fragment_matches_its_own_recent_caption_not_last_other_speaker(self):
        tracker=SubtitleTracker(require_named=True)
        tracker.label_context={'местные жители':('не бойся мы рядом',1),
                               'лусия':('хорошо идем',1)}
        tracker.last_speaker='лусия'
        cues=self.accept(tracker,[['','мы рядом.']],1.1)
        self.assertEqual(cues[0].speaker,'местные жители')
        self.assertEqual(self.accept(tracker,[['','новая фраза']],1.5),[])
        self.assertEqual(self.accept(tracker,[['','мы рядом.']],5),[])

    def test_stray_leading_colon_does_not_send_kyle_name_to_tts(self):
        for text in (': Кайл: Так, я на месте.', '.. : Кайл: Так, я на месте.'):
            self.assertEqual(split_speaker(text,('кайл',)),('Кайл','Так, я на месте.'))
        self.assertEqual(split_speaker('Кайл, стой!',('кайл',)),('', 'Кайл, стой!'))

    def test_gender_roles_do_not_merge_and_female_fallback_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,LOCALAPPDATA=tmp):
            root=Path(tmp)/'GameVoice';root.mkdir()
            path=root/'elevenlabs-voices.json'
            path.write_text(json.dumps({'speakers':[
                {'name':'Охранник','voice':'iP95p4xoKVk53GoZ742B','manual':False}],
                'aliases':{'охранница':'охранник'}}),encoding='utf8')
            r=registry()
            self.assertEqual(r.resolve('Охранница'),'охранница')
            self.assertNotEqual(r.voice_for('Охранница'),r.voice_for('Охранник'))
            self.assertEqual(r.voice_for('Охранница'),'21m00Tcm4TlvDq8ikWAM')
            r.assign('Сотрудница','MF3mGyEYCl7XYWbV9V6O')
            r.save();again=registry()
            self.assertEqual(again.voice_for('Охранница'),r.voice_for('Охранница'))
            self.assertEqual(again.voice_for('Сотрудница'),'MF3mGyEYCl7XYWbV9V6O')
        self.assertEqual(resolve_speaker_key('Сотрудница полиции',('сотрудник полиции',)),
                         'сотрудница полиции')


if __name__=='__main__':unittest.main()
