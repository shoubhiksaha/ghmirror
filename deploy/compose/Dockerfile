# ghmirror engine image. Build from the repository root:
#   docker build -f deploy/compose/Dockerfile -t ghmirror .
# Base image pinned by digest (python:3.15-slim, 2026-11-11) so every build starts from the same bytes.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
RUN apt-get update \
 && apt-get install -y --no-install-recommends git git-lfs openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && git lfs install --system
WORKDIR /app
COPY engine/ /app/
COPY deploy/compose/entrypoint.sh /usr/local/bin/ghmirror-entrypoint
# /data/mirrors and /data/archive exist in the image so new named volumes inherit the ownership
# requirements.lock pins every Python dependency to the tested version
RUN pip install --no-cache-dir -c requirements.lock '.[postgres]' && useradd -m -u 1000 ghmirror \
 && mkdir -p /data/mirrors /data/archive /secrets && chown -R ghmirror /data /secrets
VOLUME ["/data"]
EXPOSE 8080
ENTRYPOINT ["ghmirror-entrypoint"]
CMD ["run"]
