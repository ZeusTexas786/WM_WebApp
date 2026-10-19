from django.test import SimpleTestCase,RequestFactory,override_settings
from django.http import HttpResponse

class ProxyTests(SimpleTestCase):
    @override_settings(TRUSTED_PROXY_NETWORKS=('172.30.0.0/24',))
    def test_untrusted_forwarded_headers_are_stripped(self):
        from recruiting.proxy import TrustedProxy
        request=RequestFactory().get('/',REMOTE_ADDR='198.51.100.1',HTTP_X_FORWARDED_FOR='8.8.8.8',HTTP_X_FORWARDED_PROTO='https')
        TrustedProxy(lambda r:HttpResponse())(request)
        self.assertEqual(request.META['REMOTE_ADDR'],'198.51.100.1')
        self.assertNotIn('HTTP_X_FORWARDED_PROTO',request.META)

    @override_settings(TRUSTED_PROXY_NETWORKS=('172.30.0.0/24',))
    def test_trusted_proxy_must_supply_one_valid_client_address(self):
        from recruiting.proxy import TrustedProxy
        for address,expected in [('8.8.8.8','8.8.8.8'),('8.8.8.8, 1.1.1.1','172.30.0.10')]:
            request=RequestFactory().get('/',REMOTE_ADDR='172.30.0.10',HTTP_X_FORWARDED_FOR=address,HTTP_X_FORWARDED_PROTO='https')
            TrustedProxy(lambda r:HttpResponse())(request)
            self.assertEqual(request.META['REMOTE_ADDR'],expected)

class StreamingTests(SimpleTestCase):
    def test_streaming_file_limit_stops_oversized_body(self):
        from recruiting.upload_limits import LimitedUploadHandler
        from django.core.files.uploadhandler import StopUpload
        handler=LimitedUploadHandler(); handler.new_file('files','resume.txt','text/plain',None)
        with self.assertRaises(StopUpload): handler.receive_data_chunk(b'x'*(10*1024*1024+1),0)

    def test_health_live_does_not_expose_configuration(self):
        response=self.client.get('/health/live/')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{'status':'alive'})
