import ipaddress
from django.conf import settings

class TrustedProxy:
    def __init__(self,get_response): self.get_response=get_response
    def __call__(self,request):
        try:
            peer=ipaddress.ip_address(request.META.get('REMOTE_ADDR',''))
            trusted=any(peer in ipaddress.ip_network(net) for net in settings.TRUSTED_PROXY_NETWORKS)
        except ValueError: trusted=False
        if trusted:
            try: request.META['REMOTE_ADDR']=str(ipaddress.ip_address(request.META.get('HTTP_X_FORWARDED_FOR','')))
            except ValueError: pass
        else:
            request.META.pop('HTTP_X_FORWARDED_PROTO',None)
        request.META.pop('HTTP_X_FORWARDED_FOR',None)
        request.META.pop('HTTP_X_FORWARDED_HOST',None)
        return self.get_response(request)
