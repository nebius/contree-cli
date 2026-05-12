FROM python:3.12-alpine

ARG GREETING=hello
ENV APP_GREETING=${GREETING}

WORKDIR /app

COPY hello.py /app/hello.py
COPY src /app/src

RUN python -c "import sys; print('python', sys.version)"
RUN python /app/hello.py
