# syntax=docker/dockerfile:1.7
#
# trip-vlog on Ubuntu 24.04 LTS (linux/amd64 and linux/arm64). See docs/DOCKER.md.
#
#   docker build -t trip-vlog .
#   docker run --rm trip-vlog --version
#
# Not in the image: FilmCraft (mount the binary and set FILMCRAFT_CLI), DaVinci Resolve (separate machine), API keys (pass at run time).

ARG UBUNTU_VERSION=24.04

# ---- build stage: wheel for the project and its dependencies, installed into a venv -------------------------------
FROM ubuntu:${UBUNTU_VERSION} AS build

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Optional extras, comma separated, e.g. --build-arg EXTRAS=otio (adds OpenTimelineIO). Empty = core dependencies only.
ARG EXTRAS=""

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# Pillow, pydantic and the other dependencies ship manylinux wheels for both amd64 and arm64, so no compiler is needed.
# Optional build secret `ca_bundle`: extra CA certificates for pip behind a TLS-inspecting proxy
# (docker build --secret id=ca_bundle,src=/path/ca.pem .). Not stored in the image.
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -s /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi \
 && python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir ".${EXTRAS:+[$EXTRAS]}"

# ---- runtime stage -----------------------------------------------------------------------------------------------
FROM ubuntu:${UBUNTU_VERSION} AS runtime

ENV DEBIAN_FRONTEND=noninteractive
# python3 must be the same minor version as in the build stage (the venv links to /usr/bin/python3); both come from the same base image.
# ffmpeg (Ubuntu 24.04: 6.1) provides ffmpeg and ffprobe. No font package: the tool only uses Pillow's built-in font.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ffmpeg ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Non-root user. The ubuntu base image already has "ubuntu" (uid 1000); replace it so uid/gid 1000 are ours.
# Override with `--user UID:GID` (compose: `user:`) to match the owner of your media and workspace folders.
RUN userdel -r ubuntu 2>/dev/null || true \
 && groupadd --gid 1000 vlog \
 && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin vlog \
 && mkdir -p /media /work /opt/filmcraft \
 && chown vlog:vlog /work && chmod 1777 /work

COPY --from=build /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    FFMPEG=/usr/bin/ffmpeg \
    FFPROBE=/usr/bin/ffprobe
# FILMCRAFT_CLI is deliberately unset: mount a Linux filmcraft-cli and set it, e.g.
#   -v /opt/filmcraft/filmcraft-cli:/opt/filmcraft/filmcraft-cli:ro -e FILMCRAFT_CLI=/opt/filmcraft/filmcraft-cli

# /media: your photos and videos (mount read-only). /work: the output workspace (must be outside /media).
VOLUME ["/media", "/work"]
WORKDIR /work

USER 1000:1000

# tini reaps the ffmpeg / filmcraft-cli child processes and forwards signals (docker stop).
ENTRYPOINT ["/usr/bin/tini", "--", "trip-vlog"]
CMD ["--help"]
