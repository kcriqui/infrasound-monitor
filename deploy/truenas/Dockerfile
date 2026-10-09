# Analysis image for the TrueNAS SCALE box. The Pi is a data logger; everything
# expensive runs here, where there is RAM to spare.
#
# Built from the repo root:  docker build -f deploy/truenas/Dockerfile .
FROM python:3.12-slim

# rsync+ssh to pull the archive from the Pi, git to push the built site.
# No build-essential: numpy/scipy/obspy/matplotlib all ship manylinux wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git rsync openssh-client ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/infrasound-monitor
COPY . /opt/infrasound-monitor
RUN pip install --no-cache-dir -e .

# config.py checks $INFRASOUND_CONFIG first, so station identity lives in the
# dataset next to the data rather than being baked into the image.
ENV INFRASOUND_CONFIG=/data/config.toml \
    PYTHONUNBUFFERED=1

CMD ["bash", "deploy/truenas/run-loop.sh"]
