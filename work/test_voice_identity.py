"""Observed wrong-voice routes, without capture, synthesis, or real settings."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from speaker_voices import SpeakerVoices, resolve_trusted_speaker_key
from subtitle_voice import SubtitleTracker


class VoiceIdentityTests(unittest.TestCase):
    def registry(self, path):
        rows = [('Доббс','dobbs',True),('Кэл','cal',True),('Кайл','kyle',True),
                ('Лусия','lucia',True),('Луис','luis',True),('Маттиа','mattia',True),
                ('д0ббс','default',False),('ы кэл','default',False),
                ('мапиа','default',False),('Пегги','mattia',False)]
        path.write_text(json.dumps({'default_voice':'default','speakers':[
            {'name':n,'voice':v,'manual':m} for n,v,m in rows]}),encoding='utf8')
        return SpeakerVoices(path, voices=('default','dobbs','cal','kyle','lucia','luis','mattia'),
                             seeds={},fallback_voices=('default',))

    def test_saved_ocr_errors_cannot_shadow_manual_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'voices.json'; r=self.registry(path)
            for label,voice in [('д0ббс','dobbs'),('ы кэл','cal'),('мапиа','mattia')]:
                self.assertEqual(r.voice_for(label),voice)
            r.save()
            restarted=SpeakerVoices(path,voices=r.available_voices,seeds={},fallback_voices=('default',))
            self.assertEqual(restarted.voice_for('д0ббс'),'dobbs')
            self.assertEqual(restarted.voice_for('мапиа'),'mattia')

    def test_similar_real_names_remain_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=self.registry(Path(tmp)/'voices.json')
            for name,voice in [('Кэл','cal'),('Кайл','kyle'),('Лусия','lucia'),('Луис','luis')]:
                self.assertEqual(r.voice_for(name),voice)
            self.assertEqual(r.resolve('Кэт'),'кэт')
            self.assertEqual(resolve_trusted_speaker_key('Полицейский 2',{'полицейский 1'}),'полицейский 2')
            self.assertEqual(resolve_trusted_speaker_key('Дорин',{'лори'}),'дорин')

    def test_exact_manual_assignment_wins_even_if_similar_to_another(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=self.registry(Path(tmp)/'voices.json')
            r.assign('Мапиа','default')
            self.assertEqual(r.voice_for('Мапиа'),'default')

    def test_automatic_only_character_is_also_an_identity_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=self.registry(Path(tmp)/'voices.json')
            del r.entries['маттиа']
            r.automatic={'маттиа':{'name':'Маттиа','voice_id':'mattia'}}
            self.assertEqual(r.voice_for('мапиа'),'mattia')

    def test_unknown_names_never_allocate_or_reuse_another_characters_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=self.registry(Path(tmp)/'voices.json')
            self.assertEqual(r.voice_for('Новый персонаж'),'default')
            self.assertEqual(r.voice_for('Пегги'),'default')
            r.assign('Пегги','mattia')
            self.assertEqual(r.voice_for('Пегги'),'mattia')

    def test_tracker_and_voice_selection_use_same_identity_for_different_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=self.registry(Path(tmp)/'voices.json')
            tracker=SubtitleTracker(r.effective_entries(),resolver=r.resolve)
            cues=[]
            for label,text,start in [('Доббс','Первая полная реплика.',0),
                                     ('д0ббс','Совсем другая реплика.',2),
                                     ('Кэл','Хорошо, я приду завтра.',4),
                                     ('ы кэл','А сейчас поедем домой.',6)]:
                for offset in (0,.05,.1,.15,.2):
                    cues.extend(tracker.update_frame([(label,text)],start+offset))
            self.assertEqual([v.speaker for v in cues],['доббс','доббс','кэл','кэл'])
            self.assertEqual([r.voice_for(v.speaker) for v in cues],['dobbs','dobbs','cal','cal'])


if __name__=='__main__': unittest.main()
