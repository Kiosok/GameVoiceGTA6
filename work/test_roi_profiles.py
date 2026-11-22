import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outputs/GameVoice'))
import device_settings as devices
from subtitle_voice import RussianOCR, App

class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,LOCALAPPDATA=self.temp.name);self.env.start()
    def tearDown(self):
        self.env.stop();self.temp.cleanup()

    def test_legacy_medium_survives_three_profile_roundtrip(self):
        custom=(.15,.78,.85,.92)
        devices.save_setting('roi',custom);devices.save_setting('output','test speaker')
        devices.save_roi_profile('small')
        self.assertEqual(devices.load_roi(None),devices.ROI_DEFAULTS['small'])
        self.assertEqual(devices.roi_profiles()['medium'],custom)
        self.assertEqual(devices.save_roi_profile('medium'),custom)
        devices.save_roi_profile('large');devices.save_roi_profile('small')
        self.assertEqual(devices.settings()['output'],'test speaker')
        self.assertEqual(devices.selected_roi_profile(),'small')

    def test_manual_resize_changes_only_selected_profile(self):
        devices.save_roi_profile('small')
        custom=(.19,.82,.81,.914)
        devices.save_roi_profile('small',custom)
        self.assertEqual(devices.roi_profiles()['small'],custom)
        self.assertEqual(devices.roi_profiles()['medium'],devices.ROI_DEFAULTS['medium'])
        with self.assertRaises(ValueError):devices.save_roi_profile('small',(0,0,0,0))
        self.assertEqual(devices.roi_profiles()['small'],custom)

    def test_old_two_row_preset_expands_without_changing_other_sizes(self):
        old=(.16049382716049382,600/720,.8320987654320988,658/720)
        devices.save_setting('roi_profile','small')
        devices.save_setting('roi_profiles',{'small':old,'medium':devices.ROI_DEFAULTS['medium']})
        self.assertEqual(devices.load_roi(None),devices.ROI_DEFAULTS['small'])
        self.assertEqual(devices.roi_profiles()['medium'],devices.ROI_DEFAULTS['medium'])

    def test_corrupt_profile_falls_back_and_other_modes_keep_original_ocr(self):
        devices.save_setting('roi_profile',[])
        devices.save_setting('roi_profiles',{'small':[1,2,3,4]})
        self.assertEqual(devices.load_roi(None),devices.ROI_DEFAULTS['medium'])
        self.assertEqual(devices.roi_profiles()['small'],devices.ROI_DEFAULTS['small'])
        self.assertEqual(devices.roi_ocr_options('small'),(2,10))
        self.assertEqual(devices.roi_ocr_options('medium'),(1,17))

    def test_profile_change_does_not_interrupt_current_phrase(self):
        app=App.__new__(App)
        app.roi_profile=SimpleNamespace(get=lambda:'Маленькие')
        app.observer=SimpleNamespace(roi_profile='medium')
        app.status=SimpleNamespace(set=lambda text:None)
        app.new_gate=lambda:'new gate'
        app.change_roi_profile()
        self.assertEqual(app.observer.roi,devices.ROI_DEFAULTS['small'])
        self.assertEqual(app.observer.roi_profile,'small')

class ScaledOCRTests(unittest.TestCase):
    def test_boxes_and_colour_sampling_stay_in_original_frame_coordinates(self):
        rect=SimpleNamespace(x=600,y=60,width=200,height=28)
        word=SimpleNamespace(text='Лусия:',bounding_rect=rect)
        line=SimpleNamespace(text='Лусия: Привет',words=[word])
        class Engine:
            async def recognize_async(self,bitmap):return SimpleNamespace(lines=[line])
        ocr=RussianOCR.__new__(RussianOCR);ocr.engine=Engine()
        frame=Image.new('RGB',(1280,720));roi=devices.ROI_DEFAULTS['small']
        with patch('subtitle_voice.coloured_word',return_value=True) as colour:
            text,lines,_=asyncio.run(ocr.recognize(frame,roi,scale=2,min_line_height=10))
            expected=[round(roi[0]*1280)+300,round(roi[1]*720)+30,
                      round(roi[0]*1280)+400,round(roi[1]*720)+44]
            self.assertEqual(lines[0]['box'],expected)
            self.assertEqual(colour.call_args.args[1],expected)
            self.assertEqual(text,line.text)
            self.assertEqual(asyncio.run(ocr.recognize(frame,roi,scale=2,min_line_height=17))[0],'')

if __name__=='__main__':unittest.main()
