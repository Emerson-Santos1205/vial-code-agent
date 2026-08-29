ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

RUN if python -c "import sys; raise SystemExit(sys.version_info[0] != 2)"; then \
        printf 'deb http://archive.debian.org/debian buster main\n' > /etc/apt/sources.list; \
    fi && \
    apt-get -o Acquire::Check-Valid-Until=false update && \
    apt-get install -y --no-install-recommends \
    build-essential git && \
    rm -rf /var/lib/apt/lists/* && \
    if python -c "import sys; raise SystemExit(sys.version_info[0] != 2)"; then \
        python -m pip install --no-cache-dir "setuptools<45" "wheel<0.35" "pytest<5"; \
    else \
        python -m pip install --no-cache-dir "setuptools>=68" wheel "pytest<8"; \
    fi

WORKDIR /workspace
