# ghmirror engine image. Build from the repository root:
#   docker build -f deploy/compose/Dockerfile -t ghmirror .
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git git-lfs openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && git lfs install --system
WORKDIR /app
COPY engine/ /app/
COPY deploy/compose/entrypoint.sh /usr/local/bin/ghmirror-entrypoint
# /data/mirrors and /data/archive exist in the image so new named volumes inherit the ownership
RUN pip install --no-cache-dir '.[postgres]' && useradd -m -u 1000 ghmirror \
 && mkdir -p /data/mirrors /data/archive /secrets && chown -R ghmirror /data /secrets
VOLUME ["/data"]
EXPOSE 8080
ENTRYPOINT ["ghmirror-entrypoint"]
CMD ["run"]
