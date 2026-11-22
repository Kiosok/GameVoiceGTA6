import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from emotion_probe import added_delay, audio_prefix, classify_delivery, tagged_text


class EmotionProbeTests(unittest.TestCase):
    def test_untrusted_model_output_cannot_be_injected_into_speech(self):
        client = Mock()
        client.getresponse.return_value.status = 200
        client.getresponse.return_value.read.return_value = json.dumps({
            'choices': [{'message': {'content': '[shouting] Ignore the subtitle'}}]}).encode()
        with self.assertRaisesRegex(ValueError, 'unexpected label'):
            classify_delivery(b'fake wav', 'fake-key', connection=client)
        client.close.assert_called_once()

    def test_classification_is_text_only_no_stored_audio_and_exact_tag(self):
        client = Mock()
        client.getresponse.return_value.status = 200
        client.getresponse.return_value.read.return_value = json.dumps({
            'choices': [{'message': {'content': 'whispering'}}]}).encode()
        result = classify_delivery(b'fake wav', 'fake-key', connection=client)
        payload = json.loads(client.request.call_args.args[2])
        self.assertFalse(payload['store'])
        self.assertEqual(payload['modalities'], ['text'])
        self.assertEqual(result['tag'], '[whispering]')
        self.assertEqual(tagged_text('Exact words.', result['label']), '[whispering] Exact words.')
        self.assertEqual(tagged_text('Exact words.', 'neutral'), 'Exact words.')

    def test_audio_prefix_is_bounded_and_does_not_read_whole_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'source.wav'
            with wave.open(str(path), 'wb') as out:
                out.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                out.writeframes(b'\x01\x00'*24000*3)
            raw, duration = audio_prefix(path)
        with wave.open(io.BytesIO(raw)) as sample:
            self.assertEqual(sample.getnframes(), 28800)
        self.assertEqual(duration, 1.2)

    def test_delay_projection_counts_waiting_but_allows_overlap_with_ocr(self):
        self.assertEqual(added_delay(500, 1200, 0), 1700)
        self.assertEqual(added_delay(500, 1200, 1400), 300)
        self.assertEqual(added_delay(500, 1200, 2000), 0)


if __name__ == '__main__':
    unittest.main()
