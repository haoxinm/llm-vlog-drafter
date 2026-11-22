# Running trip-vlog in Docker (Linux server / NAS)

The image is built on `ubuntu:24.04` (Python 3.12, FFmpeg 6.1) and contains `trip-vlog`, `ffmpeg` and `ffprobe`. It runs as a non-root user and builds for
`linux/amd64` and `linux/arm64` (most x86 and ARM NAS boxes). Nothing is downloaded at build time except Ubuntu packages and the project's Python wheels,
all of which exist for both architectures.

What is **not** in the image, on purpose:

| Thing | Why | What to do |
|---|---|---|
| API keys | secrets must not be baked into layers | pass at run time (`-e`, `--env-file`, compose `env_file`) |
| FilmCraft (`filmcraft-cli`) | no official Linux container build; it is a separate Rust program with its own version pin | mount a Linux binary of the same CPU architecture and set `FILMCRAFT_CLI` (below) |
| DaVinci Resolve | Resolve Studio is a desktop application; `resolve-finish` needs its scripting API on the machine that runs Resolve | run `draft` in Docker, copy `<workspace>/handoff/` to the Resolve machine, run `trip-vlog resolve-finish` there |
| GPU / hardware encoding | not configured | software encoding via FFmpeg only |

## Build

```sh
docker build -t trip-vlog .
docker run --rm trip-vlog --version        # the default CMD is --help
```

Optional extras are a build argument (comma separated, same names as in `pyproject.toml`):

```sh
docker build --build-arg EXTRAS=otio -t trip-vlog:otio .   # adds OpenTimelineIO for the extra timeline.otio check
```

Behind a TLS-inspecting proxy, give pip the proxy's CA certificate as a build secret (it is not stored in the image):

```sh
docker build --secret id=ca_bundle,src=/path/to/ca.pem -t trip-vlog .
```

### Multi-arch with buildx

```sh
docker buildx create --use --name tripvlog          # once
# one image for both architectures, pushed to a registry you control
docker buildx build --platform linux/amd64,linux/arm64 -t registry.example.com/you/trip-vlog:0.1.0 --push .
# or build for the NAS architecture only and load it locally
docker buildx build --platform linux/arm64 -t trip-vlog --load .
```

Building a foreign architecture on an x86 machine needs QEMU emulation (`docker run --privileged --rm tonistiigi/binfmt --install arm64`) and is slow;
building directly on the NAS (`docker build .`) always produces the right architecture. The Dockerfile has no architecture-specific steps.

## Volumes and user

| Path | Purpose |
|---|---|
| `/media` | your photos and videos. Mount **read-only** (`:ro`); originals are never modified |
| `/work` | the output workspace (plans, thumbnails, preview, QC, Resolve handoff). Must not be inside `/media`; this is the working directory |
| `/opt/filmcraft` | optional mount point for a `filmcraft-cli` binary |

The container user is `vlog` (uid/gid 1000). Files written to a bind-mounted `/work` get the uid/gid the process runs as, so on a NAS run with the owner of the
share: `--user "$(id -u):$(id -g)"` or `user: "1026:100"` in compose (Synology typically uses a user id around 1026 and group 100 `users`). Any uid works;
`HOME` is `/tmp` and nothing needs a home directory. The image has no PUID/PGID environment mechanism (that needs a root entrypoint); use `--user` instead.

## Run

Always keep the workspace on a bind mount or named volume, otherwise it disappears with the container (`--rm`).

```sh
docker run --rm --user "$(id -u):$(id -g)" \
  -v /volume1/photos/trip-2025:/media:ro \
  -v /volume1/vlogs:/work \
  trip-vlog index --media /media --output /work/trip-2025

# the one-shot draft; stops for approval after the plan (exit code 3), as on any other platform
docker run --rm --user "$(id -u):$(id -g)" -v /volume1/photos/trip-2025:/media:ro -v /volume1/vlogs:/work \
  -e FILMCRAFT_CLI=/opt/filmcraft/filmcraft-cli -v /opt/filmcraft:/opt/filmcraft:ro \
  trip-vlog draft --media /media --output /work/trip-2025 --duration 180

docker run --rm ... trip-vlog approve --output /work/trip-2025
docker run --rm ... trip-vlog resume  --output /work/trip-2025
```

Use `-it` when a command prompts. The entrypoint is `tini -- trip-vlog`, so everything after the image name is a `trip-vlog` argument. For a shell, or to
run another tool from the image: `docker run --rm -it --entrypoint bash trip-vlog`, `--entrypoint ffprobe`.

### docker compose

`docker-compose.yml` is an example for a server or NAS. This is a command line tool, not a daemon, so it has no `restart:` policy and you do not use `up`:

```sh
cp docker.env.example .env.docker                      # keys; ignored by git
export MEDIA_DIR=/volume1/photos/trip-2025 WORK_DIR=/volume1/vlogs PUID=$(id -u) PGID=$(id -g)
docker compose build
docker compose run --rm trip-vlog index --media /media --output /work/trip-2025
docker compose run --rm trip-vlog draft --media /media --output /work/trip-2025 --duration 180
```

## Environment variables

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, ... | whichever `api_key_env` your config names (see README "LLM provider"). Never written to the image, config or logs |
| `TRIP_VLOG_LLM_MODEL`, `TRIP_VLOG_SPEECH_MODEL` | default model-name variables (`model_env`) |
| `FILMCRAFT_CLI` | path of `filmcraft-cli` inside the container. Not set by default |
| `FFMPEG`, `FFPROBE` | preset to `/usr/bin/ffmpeg` and `/usr/bin/ffprobe` |
| `TRIP_VLOG_TRUST_WORKSPACE_CONFIG` | `1` to honor a `project.yaml` found in the workspace (ignored with a warning otherwise; see README) |
| `TZ` | time zone for timestamps in logs |

Project config files: put `project.yaml` next to the workspace on a mounted path and pass `--config /work/project.yaml`. Credentials are only ever referenced by
variable *name* in the config, so the same file works inside and outside Docker.

## Limitations and notes

- **FilmCraft is required for `draft` / `resume`** (assembly, preview). `index`, `analyze`, `transcribe`, `outline`, `llm-check`, `status` work without it.
  The binary must be a Linux build for the container's CPU architecture (a glibc build runs on the Ubuntu image; a macOS or Windows binary does not) and must
  find its own dependencies (FFmpeg libraries are in the image). Pinned/tested version: see the README.
- **Resolve handoff is a file package.** `draft` writes the handoff package (OTIO/FCPXML/EDL, notes) into the workspace; open it in Resolve on the editing machine.
  `resolve-finish` cannot run in the container.
- **Local LLM / whisper servers on the Docker host.** The `localhost` inside the container is the container. Point `base_url` at `http://host.docker.internal:11434`
  and uncomment `extra_hosts: host.docker.internal:host-gateway` in the compose file (or `--add-host`). Such a host is not loopback, so the config needs
  `allow_insecure_http: true` for that endpoint and cannot be combined with `media.local_only: true` (which only accepts 127.x / ::1 / localhost). Alternatively
  use `--network host` on a Linux server, which keeps `localhost` valid.
- **Network:** no ports are published. Outbound HTTPS is only needed for a remote LLM endpoint.
- The image is about 900 MB because Ubuntu's `ffmpeg` package pulls in many codec libraries.
- Fonts: the tool only uses Pillow's built-in font (thumbnails, contact sheets), so no font package is installed. Titles are rendered by FilmCraft, not by the image.
- Tests are not included in the image. The suite was run against the image by mounting `tests/`, `tools/`, `spike/`, `schemas/`, `examples/` and `pyproject.toml`
  of a checkout into a work directory and a pytest installed with `pip install --target`; two tests depend on the host layout (one expects a real parent
  process, one runs a script from a checkout without the package installed) and are not meaningful there. Run the suite on the host as in the README.
