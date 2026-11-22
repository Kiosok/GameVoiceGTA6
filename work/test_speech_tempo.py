import array
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from speech_tempo import choose_tempo, change_tempo, RATE


class TempoTests(unittest.TestCase):
    def test_backlog_increases_tempo_with_bound_and_manual_override(self):
        self.assertEqual(choose_tempo(1.25,True,1,1),1.25)
        self.assertEqual(choose_tempo(1.25,True,3,1),1.4)
        self.assertEqual(choose_tempo(1.25,True,1,8),1.5)
        self.assertEqual(choose_tempo(1.15,False,10,20),1.15)
        self.assertEqual(choose_tempo(9,True,1,0),1.5)

    def test_real_time_stretch_shortens_audio_and_preserves_pitch(self):
        source=array.array('h',(round(10000*math.sin(2*math.pi*1000*n/RATE))
                                for n in range(RATE*3))).tobytes()
        for factor in (1.25,1.5):
            with self.subTest(factor=factor):
                output=change_tempo(source,factor)
                self.assertAlmostEqual(len(output)/len(source),1/factor,delta=.025)
                samples=array.array('h');samples.frombytes(output)
                crossings=sum(a<=0<b for a,b in zip(samples,samples[1:]))
                frequency=crossings*RATE/len(samples)
                self.assertAlmostEqual(frequency,1000,delta=5)


if __name__=='__main__': unittest.main()
