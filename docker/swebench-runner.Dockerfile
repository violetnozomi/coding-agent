FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 安装 git 和 ca-certificates（运行时通过 -v /usr/bin/docker:/usr/bin/docker 挂载宿主机 docker CLI）
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work

COPY requirements.txt /tmp/nz-coder-requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /tmp/nz-coder-requirements.txt \
    && pip install swebench datasets docker

CMD ["python", "-m", "nz_coder.swebench_lite", "check"]
