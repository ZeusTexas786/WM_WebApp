import os
# Mark before importing shared settings so .env and generated secrets are never read.
os.environ['WEBMONKIE_LOCAL']='0'
from .settings import *
from .production_config import load_production_config
globals().update(load_production_config(os.environ))
MIDDLEWARE=['recruiting.proxy.TrustedProxy']+MIDDLEWARE
SECURE_REDIRECT_EXEMPT=[r'^health/live/$',r'^health/ready/$']
