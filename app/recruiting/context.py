from django.conf import settings

def runtime(request):
    return {'local_development': settings.LOCAL_DEVELOPMENT,
            'automatic_workflow_enabled': settings.AUTOMATIC_WORKFLOW_ENABLED,
            'fixture_pilot': settings.FIXTURE_PILOT,
            'ai_configured': bool(settings.AI_API_KEY) and settings.AI_MAX_DAILY_REQUESTS > 0,
            'smtp_configured': bool(settings.EMAIL_HOST)}
