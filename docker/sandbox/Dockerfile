# Image used to execute untrusted, LLM-generated code. Build with:
#   docker build -t pbt-sandbox docker/sandbox
FROM python:3.13-slim
RUN pip install --no-cache-dir hypothesis numpy
RUN useradd --no-create-home --uid 10001 sandbox
USER sandbox
WORKDIR /tmp
