FROM python:3.9-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Keep the historical Astropy toolchain in the image.  The repository is
# mounted at runtime, so only source-specific extension compilation remains.
RUN python -m pip install --no-cache-dir --prefer-binary --disable-pip-version-check \
    "numpy<1.22" "setuptools<60" "extension-helpers<1.0" \
    "setuptools_scm<7" wheel "pyerfa<3" "PyYAML>=3.13" "Cython<3" \
    "pytest==7.4.4" "pytest-astropy==0.9.0" \
    "pytest-astropy-header==0.1.2"

ENV VIAL_SWEBENCH_ASTROPY=1
