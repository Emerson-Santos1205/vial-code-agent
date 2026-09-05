FROM python:3.8-slim

ARG REPO=django/django

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --prefer-binary \
    --disable-pip-version-check "pytest<8"

# Pre-install common dependencies by repository to avoid per-task reinstall
RUN if [ "$REPO" = "django/django" ]; then \
      python -m pip install --no-cache-dir --prefer-binary \
        --disable-pip-version-check pytz asgiref sqlparse "backports.zoneinfo"; \
    elif [ "$REPO" = "astropy/astropy" ]; then \
      python -m pip install --no-cache-dir --prefer-binary \
        --disable-pip-version-check "pytest==7.4.4" "Cython<3" \
        "pytest-astropy==0.9.0" "pytest-astropy-header==0.1.2" \
        "numpy<1.22" "setuptools<60" extension-helpers \
        setuptools_scm wheel pyerfa PyYAML; \
    fi

WORKDIR /workspace

ENV VIAL_SWEBENCH_DJANGO=1
