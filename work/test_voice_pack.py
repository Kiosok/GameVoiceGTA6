import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from voice_pack import export_pack, import_pack, install_assignments, pack_digest, read_pack
from voice_pipeline import atomic_json
from speaker_voices import speaker_key


class FakeAPI:
    def __init__(self):
        self.user = {'user_id': 'test-user', 'workspace_id': 'test-workspace',
                     'subscription': {'can_use_instant_voice_cloning': True,
                                      'voice_limit': 10, 'voice_slots_used': 0}}
        self.voices = []
        self.posts = 0
        self.failure = None
        self.verification = False

    def request(self, method, path):
        assert method == 'GET'
        if path == '/v1/user':
            return self.user
        if path.startswith('/v2/voices?'):
            return {'voices': self.voices, 'has_more': False}
        return next(v for v in self.voices if path.endswith(v['voice_id']))

    def create(self, name, files, marker):
        self.posts += 1
        voice = {'name': name, 'voice_id': str(self.posts).zfill(20), 'description': marker,
                 'category': 'cloned', 'requires_verification': self.verification}
        self.voices.append(voice)
        if self.failure:
            raise self.failure
        return {'voice_id': voice['voice_id'], 'requires_verification': self.verification}


class PackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pack = self.root/'pack'
        self.pack.mkdir()
        with wave.open(str(self.pack/'sample.wav'), 'wb') as out:
            out.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            out.writeframes(b'\x01\x00'*24000*60)
        self.digest = hashlib.sha256((self.pack/'sample.wav').read_bytes()).hexdigest()
        self.voices = [{'name': 'Character', 'clean_speech_seconds': 60,
                        'samples': [{'path': 'sample.wav', 'sha256': self.digest}]}]
        self.write_manifest()
        self.api = FakeAPI()

    def write_manifest(self):
        atomic_json(self.pack/'manifest.json', {'schema': 1, 'pack_id': pack_digest(self.voices), 'voices': self.voices})

    def run_import(self):
        return import_pack(self.pack, self.root/'state', self.api)

    def test_import_resume_creates_once_and_assigns_actual_new_id(self):
        first = self.run_import()
        second = self.run_import()
        self.assertEqual(first, second)
        self.assertEqual(self.api.posts, 1)
        app = self.root/'app'
        atomic_json(app/'elevenlabs-voices.json', {'speed': 1.35, 'speakers': [
            {'name': 'Other', 'voice': 'Z'*20, 'manual': True}], 'aliases': {}})
        install_assignments(first, app)
        data = json.loads((app/'elevenlabs-voices.json').read_text(encoding='utf8'))
        self.assertEqual(data['speed'], 1.35)
        self.assertEqual(len(data['speakers']), 2)
        self.assertEqual(data['speakers'][1]['voice'], first['voices'][speaker_key('Character')]['voice_id'])
        self.assertTrue(list(app.glob('*.before-pack-*.json')))

    def test_insufficient_slots_fail_before_any_post(self):
        self.api.user['subscription']['voice_limit'] = 0
        with self.assertRaisesRegex(ValueError, 'free voice slots'):
            self.run_import()
        self.assertEqual(self.api.posts, 0)

    def test_lost_post_response_is_reconciled_without_second_post(self):
        self.api.failure = TimeoutError('response lost')
        with self.assertRaises(TimeoutError):
            self.run_import()
        self.api.failure = None
        state = self.run_import()
        self.assertEqual(self.api.posts, 1)
        self.assertEqual(state['voices'][speaker_key('Character')]['state'], 'ready')

    def test_unknown_post_without_remote_match_never_retries(self):
        self.api.failure = TimeoutError('response lost')
        with self.assertRaises(TimeoutError):
            self.run_import()
        self.api.voices.clear(); self.api.failure = None
        with self.assertRaisesRegex(ValueError, 'account review'):
            self.run_import()
        self.assertEqual(self.api.posts, 1)

    def test_verification_blocks_installation_until_remote_confirmation(self):
        self.api.verification = True
        with self.assertRaisesRegex(ValueError, 'verification'):
            self.run_import()
        with self.assertRaisesRegex(ValueError, 'verification'):
            self.run_import()
        self.assertEqual(self.api.posts, 1)
        self.api.voices[0]['requires_verification'] = False
        self.assertEqual(self.run_import()['voices'][speaker_key('Character')]['state'], 'ready')

    def test_journals_are_separate_per_workspace_and_same_samples_recreate_locally(self):
        first = self.run_import()
        self.api.user['workspace_id'] = 'different-workspace'; self.api.voices = []
        second = self.run_import()
        self.assertNotEqual(first['account'], second['account'])
        self.assertEqual(self.api.posts, 2)

    def test_deleted_ready_voice_is_not_silently_recreated(self):
        self.run_import(); self.api.voices.clear()
        with self.assertRaisesRegex(ValueError, 'removed'):
            self.run_import()
        self.assertEqual(self.api.posts, 1)

    def test_corrupted_and_escaping_samples_are_rejected_before_network(self):
        self.voices[0]['samples'][0]['path'] = '../outside.wav'; self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'inside'):
            read_pack(self.pack)
        self.voices[0]['samples'][0]['path'] = 'sample.wav'; self.write_manifest()
        (self.pack/'sample.wav').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            read_pack(self.pack)

    def test_running_app_configuration_is_not_overwritten(self):
        state = self.run_import(); app = self.root/'app'
        atomic_json(app/'pipeline-app-status.json', {'pid': 123})
        with self.assertRaisesRegex(ValueError, 'Close GameVoice'):
            install_assignments(state, app)
        self.assertFalse((app/'elevenlabs-voices.json').exists())

    def test_export_uses_current_reference_without_owner_ids(self):
        installs = self.root/'installations'; folder = installs/'prepared'
        folder.mkdir(parents=True)
        (folder/'reference-60s.wav').write_bytes((self.pack/'sample.wav').read_bytes())
        atomic_json(folder/'installation.json', {'state': 'complete', 'name': 'Character',
                    'sha256': self.digest, 'clean_speech_seconds': 60, 'voice_id': 'PRIVATE_OWNER_ID'})
        overlay = self.root/'overlay.json'
        atomic_json(overlay, {'characters': [{'name': 'Character', 'state': 'ready',
                    'reference_sha256': self.digest, 'voice_id': 'PRIVATE_OWNER_ID'}]})
        exported = export_pack(installs, overlay, self.root/'export')
        self.assertEqual(len(exported['voices']), 1)
        self.assertNotIn('PRIVATE_OWNER_ID', json.dumps(exported))
        self.assertNotIn('reference_file', exported['voices'][0])


if __name__ == '__main__':
    unittest.main()
