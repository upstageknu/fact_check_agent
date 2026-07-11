FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      coreutils \
      curl \
      file \
      jq \
      libcurl4-openssl-dev \
      python3 \
      python3-venv \
      strace \
      time \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY runner.py /runner.py

ENTRYPOINT ["python3", "/runner.py"]
