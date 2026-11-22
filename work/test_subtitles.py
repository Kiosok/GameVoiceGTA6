import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from subtitle_voice import SubtitleGate

class GateTests(unittest.TestCase):
    def test_stability_and_ocr_jitter_do_not_repeat(self):
        g=SubtitleGate()
        self.assertIsNone(g.update('Нам пора уходить!',0))
        self.assertEqual(g.update('Нам пора уходить!',.2)[0],'Нам пора уходить!')
        self.assertIsNone(g.update('Нам пора уходить.',.4))
        self.assertIsNone(g.update('Нам пора уходить!',.6))
    def test_new_line_and_rolling_extension(self):
        g=SubtitleGate()
        g.update('Я тебя вижу.',0); g.update('Я тебя вижу.',.2)
        self.assertIsNone(g.update('Я тебя вижу. Иди сюда!',.4))
        self.assertEqual(g.update('Я тебя вижу. Иди сюда!',.6)[0],'Иди сюда!')
        g.update('Стой, не двигайся!',.8)
        self.assertEqual(g.update('Стой, не двигайся!',1)[0],'Стой, не двигайся!')
    def test_pause_and_resume_do_not_repeat_last_caption(self):
        g=SubtitleGate()
        for t in (0,.2): g.update('Ты меня слышишь?',t)
        g.update('',1)
        self.assertIsNone(g.update('Ты меня слышишь?',1.2))
        self.assertIsNone(g.update('Ты меня слышишь?',1.4))
        g.update('',7)
        g.update('Ты меня слышишь?',7.2)
        self.assertIsNone(g.update('Ты меня слышишь?',7.4))
        g.update('',3600)
        self.assertIsNone(g.update('Ты меня слышишь?',3600.2))
        self.assertIsNone(g.update('Ты меня слышишь?',3600.4))

    def test_pause_preserves_fuzzy_caption_memory(self):
        g=SubtitleGate()
        first='Нет, я просто не могу. Не буду. И не проси.'
        changed='Нет, я простоне могу. Не будуа И не проси.'
        g.update(first,0); self.assertIsNotNone(g.update(first,.2))
        g.update('',60)
        self.assertIsNone(g.update(changed,60.2))
        self.assertIsNone(g.update(changed,60.4))

    def test_new_dialogue_after_pause_and_later_actual_repeat_are_accepted(self):
        g=SubtitleGate()
        first='Ты меня слышишь?'
        other='Да, отлично слышу. Продолжай!'
        g.update(first,0); self.assertIsNotNone(g.update(first,.2))
        g.update('',60)
        g.update(other,60.2)
        self.assertEqual(g.update(other,60.4)[0],other)
        g.update('',80)
        g.update(first,80.2)
        self.assertEqual(g.update(first,80.4)[0],first)
    def test_noise_and_single_flash_not_spoken(self):
        g=SubtitleGate()
        for text in ('','1234','X','...','Map','A Б'):
            self.assertIsNone(g.update(text,0))
        g.update('Нажмите продолжить',1)
        self.assertIsNone(g.update('Привет, как дела?',1.2))

    def test_short_words_including_profanity_are_preserved(self):
        for text in ('Блядь!', 'Бля!', 'Хуй!', 'Нахуй!', 'Да.', 'Нет!'):
            with self.subTest(text=text):
                g=SubtitleGate()
                self.assertIsNone(g.update(text,0))
                self.assertIsNone(g.update(text,.05))
                self.assertEqual(g.update(text,.1)[0],text)
                self.assertIsNone(g.update(text,.15))
                g.update('',60)
                for t in (60.05,60.1,60.15):
                    self.assertIsNone(g.update(text,t))

    def test_short_flash_still_does_not_become_speech(self):
        g=SubtitleGate()
        g.update('Бля!',0);self.assertIsNone(g.update('Бля!',.05))
        g.update('',1)
        self.assertIsNone(g.update('Бля!',1.05))
    def test_live_background_jitter_is_not_a_new_caption(self):
        g=SubtitleGate()
        first='Нет, я просто не могу. Не буду. И не проси.'
        changed='Нет, я простоне могу. Не будуа И не проси.'
        g.update(first,0); self.assertIsNotNone(g.update(first,.2))
        self.assertIsNone(g.update(changed,.4))
        self.assertIsNone(g.update(changed,.6))

    def test_caption_cleaned_after_transition_is_not_spoken_twice(self):
        g=SubtitleGate()
        caption='Черт, он с кем-то говорит! Вытащи эту хрень у него из уха.'
        transition='Рядом с ветряком, судя по всему! '+caption
        g.update(transition,0);self.assertIsNotNone(g.update(transition,.2))
        self.assertIsNone(g.update(caption,.4))
        self.assertIsNone(g.update(caption,.6))

if __name__=='__main__': unittest.main()
