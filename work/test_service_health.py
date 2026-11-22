import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs/GameVoice'))
from service_health import ServiceHealth


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.health = ServiceHealth('ElevenLabs', lambda: self.now)
        self.health.event('speech_enabled', True)

    def test_region_error_survives_queue_full_idle_and_toggle(self):
        h = self.health
        h.event('speech_error', 'SpeechFailure: Доступ ограничен по региону')
        self.assertNotIn('SpeechFailure', h.view()[1])
        for kind, value in [('notice', 'Очередь заполнена'),
                            ('status', 'Голоса подключены'),
                            ('speech_enabled', False), ('speech_enabled', True)]:
            h.event(kind, value)
            self.assertEqual(h.view()[0], 'error')
            self.assertIn('регион', h.view()[1])
        h.event('synthesis', {})
        self.assertEqual(h.view()[0], 'ok')

    def test_synthesis_does_not_hide_output_failure(self):
        h = self.health
        h.event('speech_error', 'Озвучка приостановлена: отказ API')
        h.event('output_error', 'Колонка отключена')
        h.event('synthesis', {})
        self.assertIn('Колонка', h.view()[1])
        h.event('latency', 100)
        self.assertEqual(h.view()[0], 'ok')

    def test_idle_reports_last_result_without_claiming_current_connection(self):
        h = self.health
        h.event('synthesis', {})
        self.now = 600
        self.assertEqual(h.view()[0], 'ok')
        self.assertIn('последний запрос', h.view()[1])
        self.assertIn('жду', h.view()[1])

    def test_slow_request_yellow_then_real_response_green(self):
        h = self.health
        h.event('synthesis', {})
        h.event('synthesis_started', 1)
        self.now = 3
        self.assertEqual(h.view()[0], 'waiting')
        self.assertIn('3 с', h.view()[1])
        h.event('synthesis', {})
        self.assertEqual(h.view()[0], 'ok')

    def test_retry_yellow_terminal_red_and_recovery_green(self):
        h = self.health
        h.event('synthesis', {})
        h.event('connection_error', 'TimeoutError: timed out')
        self.assertEqual(h.view()[0], 'waiting')
        h.event('speech_error', 'Нет связи, озвучка приостановлена')
        self.assertEqual(h.view()[0], 'error')
        h.event('synthesis', {})
        self.assertEqual(h.view()[0], 'ok')

    def test_prepared_client_does_not_falsely_prove_connection(self):
        h = self.health
        h.event('status', 'Голоса подключены')
        self.assertEqual(h.view()[0], 'waiting')
        self.assertIn('первой реплике', h.view()[1])

    def test_first_working_request_waits_yellow_without_a_probe(self):
        h = self.health
        h.event('synthesis_started', 1)
        self.assertEqual(h.view()[0], 'waiting')
        self.assertIn('получаю голос', h.view()[1])
        h.event('synthesis', {})
        self.assertEqual(h.view()[0], 'ok')

    def test_disabled_is_neutral_not_a_connection_failure(self):
        h = self.health
        h.event('speech_enabled', False)
        self.now = 600
        self.assertEqual(h.view()[0], 'off')

    def test_ocr_success_only_clears_camera_error(self):
        h = self.health
        h.event('capture_error', 'Карта недоступна')
        h.event('speech_error', 'API отказал')
        h.event('ocr', {})
        self.assertNotIn('Карта', h.view()[1])
        self.assertIn('API отказал', h.view()[1])


if __name__ == '__main__':
    unittest.main()
