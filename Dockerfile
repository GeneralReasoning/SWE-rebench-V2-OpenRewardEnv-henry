FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt update && apt upgrade -y && apt install -y \
    software-properties-common \
    ca-certificates \
    python3 \
    python3-pip \
    curl \
    git \
    git-lfs \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

RUN uv venv --python 3.11

COPY requirements.txt /app/
# Vendored openreward SDK lives under ./vendor/openreward-sdk and is installed
# from local source (see requirements.txt). Copying it in before the pip step
# means SDK edits invalidate this layer and trigger a reinstall.
COPY vendor/ /app/vendor/
RUN uv pip install -r /app/requirements.txt

COPY . /app/

# Dataset is included via the COPY above (./data → /app/data). In prod the data
# is served from /orwd_data via a GCSFuse mount; for local runs the server
# falls back to /app/data/data.parquet when /orwd_data is empty.

EXPOSE 8080

CMD ["uv", "run", "python", "/app/server.py"]
