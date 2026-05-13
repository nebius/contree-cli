FROM python:3.12-alpine

ARG GREETING=hello
ENV APP_GREETING=${GREETING}

WORKDIR /app

COPY hello.py /app/hello.py
COPY src /app/src
ADD https://github.com/nebius/contree-cli/archive/refs/heads/master.zip /tmp/contree-cli.zip

RUN python -c "import sys; print('python', sys.version)"
RUN python -m zipfile -e /tmp/contree-cli.zip /opt/
RUN pip install --no-cache-dir /opt/contree-cli-master
RUN contree --help | head -20
RUN python /app/hello.py
