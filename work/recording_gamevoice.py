"""Temporary live diagnostic launcher; ordinary app and voice, local samples only."""
import sys,time,json,queue,threading
from pathlib import Path
base=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(base/'outputs/GameVoice'))
import subtitle_voice as game

folder=base/'work/subtitle-background-continuous';folder.mkdir(exist_ok=True)
records=queue.Queue(maxsize=3)
stop=threading.Event()
def writer():
    with (folder/'observations.jsonl').open('a',encoding='utf8') as log:
        while not stop.is_set() or not records.empty():
            try:value,item,index=records.get(timeout=.1)
            except queue.Empty:continue
            if item:
                item[2].save(folder/f'frame-{index:04}.png')
                value={**value,'image':f'frame-{index:04}.png','image_frame':item[0]}
            log.write(json.dumps(value,ensure_ascii=False)+'\n');log.flush()
thread=threading.Thread(target=writer,daemon=True);thread.start()

OriginalObserver=game.Observer
class RecordingObserver(OriginalObserver):
    def __init__(self,emit,*args,**kwargs):
        super().__init__(emit,*args,**kwargs)
        self.started=time.monotonic();self.last_save=0;self.seen=set();self.saved=0
        normal_emit=self.emit
        def record(kind,value):
            normal_emit(kind,value)
            if kind!='ocr' or self.saved>=120 or time.monotonic()-self.started>300:return
            fresh=value['text'] and value['text'] not in self.seen
            if fresh or time.monotonic()-self.last_save>3:
                try:
                    index=self.saved+1
                    records.put_nowait((value,self.capture.snapshot(),index))
                    self.saved=index;self.last_save=time.monotonic();self.seen.add(value['text'])
                except queue.Full:pass
        self.emit=record
game.Observer=RecordingObserver
try:
    sys.argv=[sys.argv[0],'--openai']
    game.main()
finally:
    stop.set();thread.join(timeout=3)
