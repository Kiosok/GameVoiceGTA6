"""Observed full labels must outrank colour fragments and polluted old aliases."""
import json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from speaker_voices import split_speaker,split_fused_label
from subtitle_labels import separate_label,separate_captions
from subtitle_voice import SubtitleTracker
from elevenlabs_speech import registry

class RoleBoundaryTests(unittest.TestCase):
    def accept(self,tracker,groups,start=0):
        return [cue for offset in (0,.05,.1,.15,.2) for cue in tracker.update_frame(groups,start+offset)]

    def test_real_labels_cannot_be_split_against_old_near_matches(self):
        known=('водители','незнакомке','местные жители','бездомная')
        for name in ('Водитель','Незнакомец','Местные жительницы','Бездомный'):
            self.assertEqual(split_fused_label(name,known),(name,''))
            self.assertEqual(split_speaker(name+': О нет!',known),(name,'О нет!'))
        # A previously unseen proper name also keeps its lowercase ending.
        self.assertEqual(split_fused_label('Валентин',('валентина',)),('Валентин',''))
        self.assertEqual(split_fused_label('ДжейсонПривет',('джейсон',)),('Джейсон','Привет'))

    def test_full_delimiter_precedes_incorrect_last_letter_colour(self):
        known=('водители','незнакомке','местные жители')
        for head,tail,name in (('Водител','ь','Водитель'),('Незнакоме','ц','Незнакомец'),
                               ('Местные жительни','цы','Местные жительницы')):
            line={'text':name+': О нет!', 'words':[
                {'text':head,'coloured':True},{'text':tail+':','coloured':False},
                {'text':'О нет!','coloured':False}]}
            self.assertEqual(separate_label(line['text'],[line],known),(name,'О нет!'))

    def test_gender_number_roles_survive_polluted_registry_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,LOCALAPPDATA=tmp):
            root=Path(tmp)/'GameVoice';root.mkdir()
            path=root/'elevenlabs-voices.json'
            path.write_text(json.dumps({'speakers':[
                {'name':'Бездомная','voice':'JBFqnCBsd6RMkjVDRZzb'},
                {'name':'Местные жители','voice':'JBFqnCBsd6RMkjVDRZzb'},
                {'name':'Незнакомке','voice':'JBFqnCBsd6RMkjVDRZzb'}],
                'aliases':{'бездомный':'бездомная','местные жительницы':'местные жители',
                           'незнакомец':'незнакомке'}}),encoding='utf8')
            r=registry()
            for name in ('Бездомный','Бездомная','Незнакомец','Незнакомка','Местные жительницы',
                         'Местные жители','Сотрудники полиции','Сотрудницы полиции','Водитель'):
                self.assertEqual(r.resolve(name),name.casefold())
            for name in ('Бездомная','Местные жительницы','Сотрудницы полиции'):
                self.assertEqual(r.voice_for(name),'21m00Tcm4TlvDq8ikWAM')
            tracker=SubtitleTracker(r.effective_entries(),resolver=r.resolve,require_named=True)
            cues=self.accept(tracker,[['Бездомная','Нет, я напугана!'],['Незнакомец','Перекрыть всё!'],
                                      ['Бездомный','О нет!']])
            self.assertEqual({c.speaker:c.text for c in cues},
                             {'бездомная':'Нет, я напугана!','незнакомец':'Перекрыть всё!','бездомный':'О нет!'})
            r.assign('Бездомная','MF3mGyEYCl7XYWbV9V6O');r.save()
            self.assertEqual(registry().voice_for('Бездомная'),'MF3mGyEYCl7XYWbV9V6O')

    def test_coloured_menu_and_legacy_names_are_not_speakers(self):
        labels=('ЦЕНА ПРОДАЖ','СТОИМОСТЬ РЕМОНТА','ПИСТОЛЕТ','РЕВОЛЬВЕР','те','эо','уд5')
        tracker=SubtitleTracker(labels,require_named=True)
        for i,name in enumerate(labels):
            text=name+' СТОИМОСТЬ РЕ'
            line={'text':text,'box':[400,610,700,630], 'words':[
                {'text':name,'coloured':True},{'text':'СТОИМОСТЬ РЕ','coloured':False}]}
            groups=separate_captions(text,[line],labels)
            self.assertEqual(self.accept(tracker,groups,i),[],name)
            # Defence also applies to recovered metadata from known old entries.
            self.assertEqual(self.accept(tracker,[[name,'СТОИМОСТЬ РЕ']],i+.3),[],name)
        self.assertEqual(self.accept(tracker,[['Лусия','Пистолет оставь здесь.']],20)[0].text,
                         'Пистолет оставь здесь.')
        self.assertEqual(self.accept(tracker,[['Ян','Заходи, я тут.']],22)[0].speaker,'ян')

if __name__=='__main__':unittest.main()
