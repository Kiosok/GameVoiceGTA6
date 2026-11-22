import sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
import key_store as k

class KeyStoreTests(unittest.TestCase):
    def test_encrypt_round_trip_overwrite_and_forget(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path=Path(folder)/'key.dpapi'
            self.assertEqual(k.load_key(path),'')
            key='test-only-not-a-real-credential'
            k.save_key(key,path)
            self.assertNotIn(key.encode(),path.read_bytes())
            self.assertEqual(k.load_key(path),key)
            k.save_key('test-replacement',path)
            self.assertEqual(k.load_key(path),'test-replacement')
            path.write_bytes(b'corrupted')
            self.assertEqual(k.load_key(path),'')
            k.forget_key(path)
            self.assertFalse(path.exists())
    def test_invalid_input_does_not_replace_saved_key(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path=Path(folder)/'key.dpapi'
            k.save_key('test-valid',path)
            for key in ('  ','one\ntwo','one\rtwo'):
                with self.assertRaises(ValueError): k.save_key(key,path)
            self.assertEqual(k.load_key(path),'test-valid')

if __name__=='__main__': unittest.main()
