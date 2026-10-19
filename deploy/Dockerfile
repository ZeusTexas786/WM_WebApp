ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/webmonkie
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements-production.txt
COPY app/manage.py app/manage.py
COPY app/config app/config
COPY app/recruiting app/recruiting
RUN groupadd -g 10001 webmonkie && useradd -u 10001 -g 10001 -M webmonkie \
    && mkdir -p /run/parser && chown 10001:10001 /run/parser \
    && WEBMONKIE_LOCAL=0 DJANGO_SECRET_KEY=build-only-static-collection DJANGO_SETTINGS_MODULE=config.settings python app/manage.py collectstatic --noinput
WORKDIR /opt/webmonkie/app
USER 10001:10001
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2", "--timeout", "45", "--worker-tmp-dir", "/tmp", "--error-logfile", "-", "--log-level", "warning"]
