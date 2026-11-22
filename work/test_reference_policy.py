import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
import wave
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from reference_policy import require_minimum_audio,require_clean_speech,speech_seconds
from elevenlabs_clones import create_ivc

def sample(seconds):
    memory=io.BytesIO()
    with wave.open(memory,'wb') as wav:
        wav.setparams((1,2,8000,0,'NONE','not compressed'))
        wav.writeframes(b'\x01\x00'*round(seconds*8000))
    return memory.getvalue()

class ReferencePolicyTests(unittest.TestCase):
    def test_short_audio_is_blocked_before_any_network_connection(self):
        connection=Mock()
        with self.assertRaisesRegex(ValueError,'Накопление'):
            create_ivc('test',[sample(59.999)],key='fake',connection_factory=connection)
        connection.assert_not_called()

    def test_duplicate_files_do_not_count_twice(self):
        raw=sample(30)
        with self.assertRaises(ValueError):require_minimum_audio([raw,raw])
        self.assertEqual(require_minimum_audio([sample(60)]),60)

    def test_silence_events_other_speakers_and_duplicate_words_do_not_count(self):
        details={'input_sha256':'source','dominant_speaker':'speaker_0','intervals':[[0,120]]}
        words=[{'type':'word','speaker_id':'speaker_0','start':0,'end':30,'text':'hello'},
            {'type':'word','speaker_id':'speaker_0','start':0,'end':30,'text':'hello'},
            {'type':'word','speaker_id':'speaker_0','start':30,'end':60,'text':'[laughs]'},
            {'type':'word','speaker_id':'speaker_1','start':60,'end':90,'text':'other'}]
        transcript={'input_sha256':'source','words':words}
        self.assertEqual(speech_seconds(details,transcript),30)
        with self.assertRaises(ValueError):require_clean_speech(details,transcript)
        words.append({'type':'word','speaker_id':'speaker_0','start':90,'end':120,'text':'goodbye'})
        self.assertEqual(require_clean_speech(details,transcript),60)

    def test_different_source_evidence_is_rejected(self):
        with self.assertRaises(ValueError):speech_seconds({'input_sha256':'a'},{'input_sha256':'b'})

if __name__=='__main__':unittest.main()
