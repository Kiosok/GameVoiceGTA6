import queue
import sys
import threading
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from subtitle_voice import SpeechWorker

class CurrentCueTests(unittest.TestCase):
    def worker(self):
        worker=SpeechWorker.__new__(SpeechWorker)
        worker.enabled=threading.Event();worker.enabled.set()
        worker.stopping=threading.Event()
        worker.queue=queue.Queue(maxsize=12)
        worker.epoch=0;worker.lock=threading.Lock()
        worker.emit=lambda *args:None
        return worker

    def test_new_caption_preserves_active_and_pending_speech(self):
        worker=self.worker()
        worker.submit('Первая реплика',0)
        active=worker.queue.get_nowait()
        self.assertTrue(worker.valid(active[2]))
        worker.submit('Вторая реплика',.2)
        worker.submit('Третья реплика',.4)
        self.assertTrue(worker.valid(active[2]))
        pending=worker.queue.get_nowait()
        self.assertEqual(pending[0],'Вторая реплика')
        self.assertTrue(worker.valid(pending[2]))
        self.assertEqual(worker.queue.get_nowait()[0],'Третья реплика')
        self.assertTrue(worker.queue.empty())

    def test_disabled_voice_does_not_queue_or_reactivate(self):
        worker=self.worker();worker.set_enabled(False)
        epoch=worker.epoch
        worker.submit('Эту реплику читать нельзя',0)
        self.assertTrue(worker.queue.empty())
        self.assertFalse(worker.valid(epoch))

if __name__=='__main__': unittest.main()
