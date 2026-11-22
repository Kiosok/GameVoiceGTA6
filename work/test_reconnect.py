import sys, unittest, threading
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
import gamevoice as g

class RetryTests(unittest.TestCase):
    def test_transient_failure_retries_but_permanent_failure_stops(self):
        made=[]; events=[]
        class Fake:
            def __init__(self,*args):
                self.capture_bytes=400; self.key=args[2]; self.emit=args[4]
                self.failure='network' if not made else 'invalid_api_key'
                self.retryable=not made; made.append(self)
            def run(self): self.key=''; self.emit('done',None)
        obj=g.ResilientEngine(key='secret',translate=True,emit=lambda *e:events.append(e))
        with patch.object(g,'Engine',Fake), patch.object(obj.stop_event,'wait',return_value=False): obj.run()
        self.assertEqual(len(made),2)
        self.assertEqual(obj.capture_bytes,800)
        self.assertEqual(obj.failure,'invalid_api_key')
        self.assertEqual(sum(e[0]=='done' for e in events),1)
        self.assertEqual(obj.key,'')
        self.assertTrue(any('попытка 1/5' in str(e) for e in events))
    def test_five_retries_then_stop(self):
        made=[]
        class Fake:
            capture_bytes=0; failure='network'; retryable=True
            def __init__(self,*a): made.append(self)
            def run(self): pass
        obj=g.ResilientEngine(key='secret',translate=True)
        with patch.object(g,'Engine',Fake), patch.object(obj.stop_event,'wait',return_value=False): obj.run()
        self.assertEqual(len(made),6)
        self.assertIsNotNone(obj.failure)
    def test_stop_during_retry_does_not_restart(self):
        class Fake:
            capture_bytes=0; failure='network'; retryable=True
            def __init__(self,*a): pass
            def run(self): pass
        obj=g.ResilientEngine(key='secret',translate=True)
        def stop_wait(delay): obj.stop(); return True
        with patch.object(g,'Engine',Fake) as factory, patch.object(obj.stop_event,'wait',side_effect=stop_wait): obj.run()
        self.assertTrue(obj.stop_event.is_set()); self.assertIsNone(obj.failure)
    def test_user_abort_reaches_active_child(self):
        obj=g.ResilientEngine(); calls=[]
        class Fake:
            def stop(self,abort=False): calls.append(abort)
        obj.child=Fake(); obj.stop(abort=True)
        self.assertEqual(calls,[True])

if __name__=='__main__': unittest.main()
