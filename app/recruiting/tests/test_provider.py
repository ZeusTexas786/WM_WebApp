import io,json
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.conf import settings

class ProviderTests(TestCase):
    @override_settings(AI_API_KEY='synthetic-test-key',AI_MAX_DAILY_REQUESTS=1)
    def test_daily_limit_and_no_response_storage(self):
        from recruiting.providers import request_json
        response={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{"ok":true}'}]}]}
        def transport(request,timeout):
            sent=json.loads(request.data)
            self.assertFalse(sent['store'])
            self.assertNotIn('tools',sent)
            self.assertLessEqual(sent['max_output_tokens'],7000)
            return io.BytesIO(json.dumps(response).encode())
        with patch('urllib.request.urlopen',side_effect=transport):
            self.assertEqual(request_json('JSON task',{'text':'untrusted'},settings.AI_EXTRACT_MODEL),{'ok':True})
            with self.assertRaisesRegex(RuntimeError,'daily'): request_json('JSON task',{},settings.AI_EXTRACT_MODEL)

    @override_settings(AI_API_KEY='synthetic-test-key',AI_MAX_DAILY_REQUESTS=100)
    def test_timeout_and_malformed_response_fail_honestly(self):
        from recruiting.providers import request_json
        with patch('urllib.request.urlopen',side_effect=TimeoutError),patch('time.sleep'):
            with self.assertRaisesRegex(RuntimeError,'could not be reached'): request_json('JSON task',{},settings.AI_EXTRACT_MODEL)
        with patch('urllib.request.urlopen',return_value=io.BytesIO(b'{}')):
            with self.assertRaisesRegex(ValueError,'incomplete'): request_json('JSON task',{},settings.AI_EXTRACT_MODEL)
