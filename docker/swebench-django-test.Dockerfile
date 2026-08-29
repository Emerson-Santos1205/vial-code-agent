FROM python:3.8-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --prefer-binary \
    --disable-pip-version-check "pytest<8"

WORKDIR /workspace

ENV VIAL_SWEBENCH_DJANGO=1
