"""The observed Kat -> Mattia routing failure, including a missed colour label."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from speaker_voices import SpeakerVoices, split_speaker
from subtitle_labels import separate_captions, separate_label
from subtitle_voice import SubtitleTracker


class LostColonTests(unittest.TestCase):
    def test_period_and_dropped_colon_recover_only_known_names(self):
        names=('кэт','кэл','кайл','маттиа')
        for raw in ('Кэт. Удачи.','Кэт.Удачи.','Кэт． Удачи.','Кэт Удачи.'):
            self.assertEqual(split_speaker(raw,names),('Кэт','Удачи.'),raw)
        for raw in ('Кэт, удачи!','Кэл собирался вчера забрать платеж.',
                    'Неизвестный. Удачи.','Я. Удачи.'):
            self.assertEqual(split_speaker(raw,names),('',raw),raw)

    def test_actual_missed_colour_no_longer_inherits_mattia_or_speaks_kat_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'voices.json'
            path.write_text(json.dumps({'speakers':[
                {'name':'Маттиа','voice':'mattia','manual':True},
                {'name':'Кэт','voice':'kat','manual':False}]}),encoding='utf8')
            registry=SpeakerVoices(path,voices=('mattia','kat'),seeds={},fallback_voices=('kat',))
            tracker=SubtitleTracker(registry.effective_entries(),resolver=registry.resolve)
            for t in (0,.2):tracker.update_frame([['Маттиа','Мы будем тут.']],t)
            # OCR lost both the colon and the colour classification in the real frame.
            line={'text':'Кэт. Удачи.','box':[600,633,687,652],'words':[
                {'text':'Кэт.','coloured':False}, {'text':'Удачи.','coloured':False}]}
            groups=separate_captions(line['text'],[line],registry.effective_entries())
            cues=[]
            for t in (1,1.05,1.1,1.15,1.2):cues.extend(tracker.update_frame(groups,t))
            self.assertEqual(len(cues),1)
            self.assertEqual((cues[0].speaker,cues[0].text),('кэт','Удачи.'))
            self.assertEqual(registry.voice_for(cues[0].speaker),'kat')

    def test_merged_coloured_name_with_period_keeps_spoken_body(self):
        line={'text':'Кэт.Удачи.','words':[{'text':'Кэт.Удачи.','coloured':True}]}
        self.assertEqual(separate_label(line['text'],[line],('кэт','кэл')),('Кэт','Удачи.'))
        line={'text':'Кэт. Удачи.','words':[{'text':'Кэт.','coloured':True},
                                                 {'text':'Удачи.','coloured':False}]}
        name,body=separate_label(line['text'],[line],('кэт','кэл'))
        self.assertEqual((name.rstrip('.'),body),('Кэт','Удачи.'))


if __name__=='__main__':unittest.main()
