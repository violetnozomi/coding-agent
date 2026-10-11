FROM ghcr.io/scaleapi/swe-bench_pro-v2@sha256:b431b3207e1399420ccf6fe4ad343951a4194dab14fa33bd1a0931e1dad83d87
COPY python /opt/nz-agent
COPY wheels /nz-wheels
COPY requirements.txt /nz-requirements.txt
RUN /opt/nz-agent/bin/python3.13 -m ensurepip && /opt/nz-agent/bin/python3.13 -m pip install --no-index --no-deps --require-hashes --target=/opt/nz-site --find-links=/nz-wheels -r /nz-requirements.txt && rm -rf /nz-wheels
