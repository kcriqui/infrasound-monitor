# Analysis image for the TrueNAS SCALE box: pulls the Pi's archive as a
# snapshotted backup and, when not pull-only, rebuilds the dashboard here.
#
# Built from the repo root:  docker build -f deploy/truenas/Dockerfile .
FROM python:3.12-slim

# rsync+ssh to pull the archive from the Pi.
# No build-essential: numpy/scipy/obspy/matplotlib all ship manylinux wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        rsync openssh-client ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/infrasound-monitor
COPY . /opt/infrasound-monitor
RUN pip install --no-cache-dir -e .

# Let a non-root, unknown uid add itself to /etc/passwd at startup (see
# entrypoint.sh). Group-writable is safe here because the container runs with
# gid 0 and nothing else shares the image.
RUN chmod g=u /etc/passwd

# config.py checks $INFRASOUND_CONFIG first, so station identity lives in the
# dataset next to the data rather than being baked into the image.
ENV INFRASOUND_CONFIG=/data/config.toml \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["bash", "/opt/infrasound-monitor/deploy/truenas/entrypoint.sh"]
CMD ["bash", "deploy/truenas/run-loop.sh"]
