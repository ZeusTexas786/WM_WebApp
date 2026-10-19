ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY services/parser/requirements.txt /parser/requirements.txt
RUN pip install --no-cache-dir -r /parser/requirements.txt \
    && groupadd -g 10001 parser && useradd -u 10001 -g 10001 -M parser \
    && mkdir -p /run/parser && chown 10001:10001 /run/parser
COPY services/parser/server.py services/parser/limited_parse.py /parser/
COPY app/recruiting/parser.py /parser/parser.py
USER 10001:10001
CMD ["python", "-I", "/parser/server.py"]
