import sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from speaker_voices import SpeakerVoices, split_speaker, resolve_speaker_key
from subtitle_voice import SubtitleGate
from openai_speech import OpenAISpeechWorker


class SpeakerTests(unittest.TestCase):
    def test_observed_labels_and_colon_inside_dialogue(self):
        self.assertEqual(split_speaker('Лусия: Ты там случаем не передумал?'),
                         ('Лусия','Ты там случаем не передумал?'))
        self.assertEqual(split_speaker('Джейсон: Скоро. Ну круто...'),
                         ('Джейсон','Скоро. Ну круто...'))
        self.assertEqual(split_speaker('Полицейский 2: Стой!'),('Полицейский 2','Стой!'))
        self.assertEqual(split_speaker('Я сказал: уходи!'),('', 'Я сказал: уходи!'))
        self.assertEqual(split_speaker('Слушай: уходи!'),('', 'Слушай: уходи!'))
        self.assertEqual(split_speaker('Лусия:'),('Лусия',''))
        self.assertEqual(split_speaker('— тайан: Итак, бутик.'),('тайан','Итак, бутик.'))

    def test_assignments_survive_restart_and_ocr_spelling_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'voices.json'; registry=SpeakerVoices(path)
            self.assertEqual(registry.voice_for('Лусия'),'marin')
            self.assertEqual(registry.voice_for('Люсия'),'marin')
            self.assertEqual(registry.voice_for('Луcия'),'marin')
            self.assertEqual(registry.voice_for('Джейсон'),'cedar')
            self.assertEqual(registry.voice_for('Охранник'),'ash')
            registry.assign('Охранник','echo')
            registry.speed=1.5;registry.save()
            restarted=SpeakerVoices(path)
            self.assertEqual(restarted.voice_for('Охранник'),'echo')
            self.assertEqual(restarted.speed,1.5)
            self.assertEqual(restarted.voice_for(''),'cedar')
            self.assertEqual(restarted.voice_for('Прохожий'),'ash')

    def test_single_glyph_name_jitter_does_not_create_voices_or_repeat(self):
        gate=SubtitleGate()
        line='Хорошо. Это хорошо. Только не перестарайся.'
        self.assertIsNotNone(self.accept(gate,'Брайан: '+line,0))
        for i,name in enumerate(('Брайаи','Брайай','Брайан'),1):
            self.assertIsNone(self.accept(gate,name+': '+line,i))
        with tempfile.TemporaryDirectory() as tmp:
            registry=SpeakerVoices(Path(tmp)/'voices.json')
            expected=registry.voice_for('Брайан')
            for name in ('Брайаи','Брайай','Брайан'):
                self.assertEqual(registry.voice_for(name),expected)
            self.assertEqual(len(registry.entries),3)
        self.assertEqual(resolve_speaker_key('Илан',{'иван'}),'илан')
        self.assertEqual(resolve_speaker_key('Брайан',{'брайаи','брайай'}),'брайан')
        self.assertEqual(resolve_speaker_key('Полицейский 2',{'полицейский 1'}),'полицейский 2')

    def accept(self, gate, text, start):
        result=None
        for offset in (0,.05,.1,.15,.2):
            accepted=gate.update(text,start+offset)
            if accepted: result=accepted
        return result

    def test_long_duplicate_ignores_noisy_speaker_but_distinct_short_replies_remain(self):
        gate=SubtitleGate()
        self.assertEqual(self.accept(gate,'Джейсон: Ты меня слышишь?',0)[0], 'Джейсон: Ты меня слышишь?')
        self.assertIsNone(self.accept(gate,'Лусия: Ты меня слышишь?',1))
        gate.update('',100)
        self.assertIsNone(self.accept(gate,'Люсия: Ты меня слышишь?',101))
        self.assertIsNone(self.accept(gate,'Ты меня слышишь?',102))
        self.assertIsNotNone(self.accept(gate,'Джейсон: Да.',103))
        self.assertIsNotNone(self.accept(gate,'Лусия: Да.',104))

    def test_extension_keeps_name_without_speaking_it_as_a_new_word(self):
        gate=SubtitleGate()
        self.accept(gate,'Джейсон: Я тебя вижу.',0)
        self.assertEqual(self.accept(gate,'Джейсон: Я тебя вижу. Иди сюда!',1)[0],
                         'Джейсон: Иди сюда!')
        self.assertIsNone(self.accept(gate,'Лусия:',2))
        self.assertIsNone(gate.update('Лусия: Да.',3))
        self.assertIsNone(gate.update('Лусия: Да.',3.05))
        self.assertEqual(gate.update('Лусия: Да.',3.1)[0],'Лусия: Да.')

    def test_voice_sessions_reused_with_fifo_and_retry_on_same_voice(self):
        calls=[];created=[];played=[];finished=threading.Event()
        class Client:
            key='fake';first_chunk_ms=1;ws=object();needs_reconnect=False
            def __init__(self,key,voice):self.voice=voice;created.append(voice)
            def maintain(self):pass
            def abort(self):pass
            def close(self):pass
            def synthesize(self,text,number):
                calls.append((self.voice,number,text))
                if self.voice=='marin' and sum(v=='marin' for v,_,_ in calls)==1:
                    raise ConnectionError('test partial phrase lost')
                return bytes([number])*960,text
        class Output:
            def __init__(self,**kw):pass
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def abort(self):pass
            def write(self,data):
                played.append(data)
                if len(played)==3:finished.set()
        with patch('openai_speech.SpeechClient',Client),patch('openai_speech.sd.RawOutputStream',Output),patch('openai_speech.change_tempo',side_effect=lambda pcm,factor:pcm):
            worker=OpenAISpeechWorker(lambda *args:None);worker.volume=1
            try:
                worker.prepare_voices(['cedar','marin']);worker.set_enabled(True)
                for voice,speaker,text in [('cedar','Джейсон','Первая'),('marin','Лусия','Вторая'),('cedar','Джейсон','Третья')]:
                    worker.submit(text,time.monotonic(),voice,speaker)
                self.assertTrue(finished.wait(4))
                self.assertEqual(created,['cedar','marin'])
                self.assertEqual([(v,n) for v,n,_ in calls],[('cedar',1),('marin',2),('marin',2),('cedar',3)])
                self.assertEqual(played,[bytes([n])*960 for n in (1,2,3)])
            finally:worker.close()


if __name__=='__main__':unittest.main()
