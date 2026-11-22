import io,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
from subtitle_voice import VideoCapture,WIDTH,HEIGHT

class FragmentedPipe:
    def __init__(self,data):self.data=io.BytesIO(data)
    def read(self,n):return self.data.read(min(n,337))

class CaptureTests(unittest.TestCase):
    def jpeg(self,colour,size=(WIDTH,HEIGHT)):
        out=io.BytesIO();Image.new('RGB',size,colour).save(out,format='JPEG');return out.getvalue()
    def test_fragmented_mjpeg_preserves_complete_frame_boundaries(self):
        events=[];capture=VideoCapture(lambda *e:events.append(e))
        capture.proc=SimpleNamespace(stdout=FragmentedPipe(b'header'+self.jpeg('red')+self.jpeg('blue')))
        capture.read()
        counter,stamp,frame=capture.snapshot()
        self.assertEqual(counter,2);self.assertEqual(frame.size,(WIDTH,HEIGHT))
        self.assertGreater(frame.getpixel((10,10))[2],245)
        self.assertEqual(capture.wait_for_frame(1)[0],2)
    def test_truncated_jpeg_is_never_published(self):
        capture=VideoCapture(lambda *e:None)
        capture.proc=SimpleNamespace(stdout=FragmentedPipe(self.jpeg('red')[:-20]))
        capture.read();self.assertIsNone(capture.snapshot())

if __name__=='__main__':unittest.main()
