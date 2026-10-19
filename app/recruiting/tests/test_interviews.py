from unittest.mock import patch
from django.test import SimpleTestCase, TestCase
from django.contrib.auth.models import User

class ResearchTests(SimpleTestCase):
    def test_fetched_but_uncited_documents_are_not_labelled_cited(self):
        from recruiting.interviews import validate_pack
        source={'url':'https://docs.python.org/3/tutorial/','title':'Python','retrieved_at':'2026-10-16'}
        q={'skill':'Python','question':'Explain a failure','answer':'Measure it','alternatives':'Other profilers','rubric':'Evidence','source_urls':[]}
        pack=validate_pack({'questions':[q]*5},[source])
        self.assertIn('not research-verified',pack['research_status'])
    def test_https_allowlist_and_private_addresses(self):
        from recruiting.interviews import validate_destination
        for url in ['http://docs.python.org/3/', 'https://localhost/', 'https://127.0.0.1/', 'https://docs.python.org.evil.test/', 'https://user:pass@docs.python.org/', 'https://docs.python.org:8443/']:
            with self.subTest(url=url), self.assertRaises(ValueError): validate_destination(url)
        with patch('socket.getaddrinfo', return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(ValueError): validate_destination('https://docs.python.org/3/')

    def test_redirect_destination_revalidated(self):
        from recruiting.interviews import fetch_documentation
        with patch('recruiting.interviews.fetch_once', return_value=(302, {'location':'http://127.0.0.1/private'}, b'')):
            with self.assertRaises(ValueError): fetch_documentation('https://docs.python.org/3/')

    def test_response_size_and_timeout(self):
        from recruiting.interviews import fetch_documentation
        with patch('recruiting.interviews.fetch_once', return_value=(200, {'content-type':'text/html'}, b'x'*600001)):
            with self.assertRaises(ValueError): fetch_documentation('https://docs.python.org/3/')
        with patch('recruiting.interviews.fetch_once', side_effect=TimeoutError):
            with self.assertRaises(ValueError): fetch_documentation('https://docs.python.org/3/')

    def test_question_bounds_answers_and_citations(self):
        from recruiting.interviews import validate_pack
        source = {'url':'https://docs.python.org/3/tutorial/', 'title':'Python tutorial','retrieved_at':'2026-10-16'}
        question = {'skill':'Python', 'question':'How would you diagnose a slow API?', 'answer':'Measure first, then profile and isolate the bottleneck.', 'alternatives':'Different profilers are acceptable.', 'rubric':'Look for measurements, a reproducible workload, and a justified intervention.', 'source_urls':[source['url']]}
        pack = {'questions':[question]*5}
        self.assertEqual(len(validate_pack(pack,[source])['questions']),5)
        for data in [{'questions':[question]*4}, {'questions':[dict(question,answer='')]*5}, {'questions':[dict(question, source_urls=['https://invented.test'])]*5}]:
            with self.assertRaises(ValueError): validate_pack(data,[source])

class PreferenceTests(TestCase):
    def test_preferences_are_explicit_and_owner_scoped(self):
        from recruiting.interviews import save_approved_preference
        from recruiting.models import Preference
        a = User.objects.create_user('a'); b = User.objects.create_user('b')
        save_approved_preference(a, 'Prioritize concrete reliability examples.')
        self.assertEqual(Preference.objects.filter(owner=b).count(),0)
        self.assertEqual(Preference.objects.get(owner=a).text,'Prioritize concrete reliability examples.')
