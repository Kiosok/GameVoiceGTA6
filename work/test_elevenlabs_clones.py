import email
from email.policy import default
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs/GameVoice'))
import elevenlabs_clones as clones
from test_elevenlabs import Connection, Response


class CloneTests(unittest.TestCase):
    def make_sample(self, path):
        with wave.open(str(path), 'wb') as out:
            out.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            out.writeframes(b'\x01\x00' * (24000*60))
        return path

    def test_multipart_preserves_files_utf8_name_and_disables_second_denoise(self):
        raw = clones.multipart('GameVoice — Брайан', [b'WAV-one', b'WAV-two'], 'example', 'boundary')
        message = email.message_from_bytes(b'Content-Type: multipart/form-data; boundary=boundary\r\n\r\n' + raw, policy=default)
        parts = list(message.iter_parts())
        self.assertEqual(parts[0].get_payload(decode=True).decode('utf8'), 'GameVoice — Брайан')
        self.assertEqual(parts[2].get_payload(decode=True), b'false')
        self.assertEqual([x.get_payload(decode=True) for x in parts[3:]], [b'WAV-one', b'WAV-two'])
        self.assertEqual([x.get_param('name', header='content-disposition') for x in parts[3:]], ['files', 'files'])

    def test_verification_unknown_is_not_treated_as_ready_and_connection_closes(self):
        connection = Connection([Response([json.dumps({'voice_id': 'A' * 20}).encode()], {'Content-Type': 'application/json'})])
        with tempfile.TemporaryDirectory() as temp:
            raw=self.make_sample(Path(temp)/'sample.wav').read_bytes()
        result = clones.create_ivc('name', [raw], key='fake', connection_factory=lambda *a, **k: connection)
        self.assertTrue(result['requires_verification'])
        self.assertTrue(connection.closed)
        self.assertEqual(connection.requests[0][0][:2], ('POST', '/v1/voices/add'))

    def test_clone_saved_once_and_reused_without_duplicate_post(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.make_sample(Path(temp) / 'sample.wav')
            creator = Mock(return_value={'voice_id': 'A' * 20, 'requires_verification': False})
            first = clones.create_reviewed_clone('Брайан', [path], reviewed=True, storage=Path(temp)/'clones', creator=creator)
            second = clones.create_reviewed_clone('Брайан', [path], reviewed=True, storage=Path(temp)/'clones', creator=creator)
            self.assertEqual(first, second)
            self.assertEqual(first['state'], 'ready')
            creator.assert_called_once()

    def test_uncertain_post_is_not_retried_and_requires_review(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.make_sample(Path(temp) / 'sample.wav')
            creator = Mock(side_effect=TimeoutError('network timeout'))
            with self.assertRaises(TimeoutError):
                clones.create_reviewed_clone('Брайан', [path], reviewed=True, storage=Path(temp)/'clones', creator=creator)
            with self.assertRaisesRegex(ValueError, 'Предыдущая попытка'):
                clones.create_reviewed_clone('Брайан', [path], reviewed=True, storage=Path(temp)/'clones', creator=creator)
            creator.assert_called_once()

    def test_unreviewed_samples_never_create_a_voice(self):
        creator = Mock()
        with self.assertRaises(ValueError):
            clones.create_reviewed_clone('name', [], creator=creator)
        creator.assert_not_called()

    def test_definitive_rejection_requires_explicit_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.make_sample(Path(temp) / 'sample.wav')
            connection = Connection([Response([b'{"detail":{"message":"plan lacks cloning"}}'],
                {'Content-Type': 'application/json'}, status=400)])
            def rejected(*args):
                return clones.create_ivc(*args, key='fake', connection_factory=lambda *a, **k: connection)
            with self.assertRaises(Exception):
                clones.create_reviewed_clone('name', [path], reviewed=True, storage=Path(temp)/'clones', creator=rejected)
            record = json.loads(next((Path(temp)/'clones').glob('*.json')).read_text(encoding='utf8'))
            self.assertEqual(record['state'], 'rejected')
            self.assertEqual(record['http_status'], 400)
            creator = Mock(return_value={'voice_id': 'A' * 20, 'requires_verification': False})
            with self.assertRaisesRegex(ValueError, 'явный повтор'):
                clones.create_reviewed_clone('name', [path], reviewed=True, storage=Path(temp)/'clones', creator=creator)
            creator.assert_not_called()
            result = clones.create_reviewed_clone('name', [path], reviewed=True,
                storage=Path(temp)/'clones', creator=creator, retry_rejected=True)
            self.assertEqual(result['state'], 'ready')
            self.assertEqual(result['attempt'], 2)

    def test_rejected_reference_can_be_repacked_only_with_explicit_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.make_sample(Path(temp) / 'sample.wav')
            error = RuntimeError('plan rejects cloning')
            error.request_rejected = True
            error.http_status = 400
            creator = Mock(side_effect=error)
            with self.assertRaises(RuntimeError):
                clones.create_reviewed_clone('name', [path], reviewed=True, storage=Path(temp)/'clones', creator=creator)
            with wave.open(str(path), 'wb') as out:
                out.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                out.writeframes(b'\x02\x00' * (24000*60))
            creator = Mock(return_value={'voice_id': 'A' * 20, 'requires_verification': False})
            with self.assertRaises(ValueError):
                clones.create_reviewed_clone('name', [path], reviewed=True, storage=Path(temp)/'clones', creator=creator)
            creator.assert_not_called()
            result = clones.create_reviewed_clone('name', [path], reviewed=True,
                storage=Path(temp)/'clones', creator=creator, retry_rejected=True)
            self.assertEqual(result['state'], 'ready')
            self.assertEqual(result['previous_rejection']['http_status'], 400)


if __name__ == '__main__':
    unittest.main()
