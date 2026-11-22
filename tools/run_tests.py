"""Run the portable offline core suite, without hardware probes or private fixtures."""
from pathlib import Path
import os
import socket
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'work'))
sys.path.insert(0, str(ROOT/'outputs/GameVoice'))
MODULES = ['test_voice_pack', 'test_emotion_probe', 'test_elevenlabs',
           'test_elevenlabs_clones', 'test_elevenlabs_v4', 'test_elevenlabs_modes',
           'test_tv_voice', 'test_output_buffer', 'test_voice_pipeline',
           'test_reference_policy', 'test_speaker_voices', 'test_voice_route_log',
           'test_openai_speech', 'test_service_health', 'test_speech_tempo']

if __name__ == '__main__':
    # Isolate keys, configuration and log rotation from a running GameVoice app.
    original_appdata = Path(os.environ.get('LOCALAPPDATA', ''))
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        try:
            candidate = (original_appdata/'Microsoft/WinGet/Links/ffmpeg.exe').resolve(strict=True)
            if candidate.is_file():
                ffmpeg = str(candidate)
        except OSError:
            pass
    if ffmpeg:
        os.environ['PATH'] = str(Path(ffmpeg).parent) + os.pathsep + os.environ.get('PATH', '')
    original_connect = socket.socket.connect

    def offline_connect(sock, address):
        # Windows asyncio uses a loopback TCP socket pair for its own wakeup pipe.
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
            return original_connect(sock, address)
        raise RuntimeError('External network disabled in offline tests')

    with tempfile.TemporaryDirectory(prefix='gamevoice-offline-tests-') as temporary:
        os.environ['LOCALAPPDATA'] = temporary
        with patch('socket.socket.connect', new=offline_connect):
            suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:] or MODULES)
            result = unittest.TextTestRunner(verbosity=1).run(suite)
        # Close Windows log handles before TemporaryDirectory removes test data.
        import logging
        logging.shutdown()
    raise SystemExit(not result.wasSuccessful())
