from django.conf import settings

def runtime(request):
    return {'local_development': settings.LOCAL_DEVELOPMENT, 'ai_configured': bool(settings.AI_API_KEY), 'smtp_configured': bool(settings.EMAIL_HOST)}
