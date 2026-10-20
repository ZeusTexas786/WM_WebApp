"""Strict HTTPS/PostgreSQL production settings for the zero-spend demo only."""
import os
os.environ['WEBMONKIE_LOCAL'] = '0'
from .settings import *
from .production_config import load_production_config
globals().update(load_production_config(os.environ, profile='fixture_pilot'))
MIDDLEWARE = ['recruiting.proxy.TrustedProxy'] + MIDDLEWARE
MIDDLEWARE.insert(MIDDLEWARE.index('django.middleware.security.SecurityMiddleware') + 1,
                  'recruiting.pilot.FixturePilotFence')
SECURE_REDIRECT_EXEMPT = [r'^health/live/$', r'^health/ready/$']
