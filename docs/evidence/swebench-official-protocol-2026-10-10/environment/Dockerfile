FROM nz-review-runtime:72579d044985
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir mpmath==1.3.0
