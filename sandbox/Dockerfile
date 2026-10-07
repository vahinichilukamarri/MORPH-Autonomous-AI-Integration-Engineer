FROM python:3.12-slim
LABEL morph.owner=morph
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install ruff==0.16.10 mypy==2.4.0
COPY runtime /tmp/runtime
RUN pip install --no-deps /tmp/runtime && rm -rf /tmp/runtime
COPY gate /opt/gate
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox
WORKDIR /app
USER 10001:10001
