# The daemon and its admin page, in a container.
#
#   docker build -t presto-ai-dash .
#   docker compose up -d
#
# Nothing private is copied into the image: the configuration file, the
# database and the encryption key file all live in the /data volume, which
# survives an upgrade of the image.
#
# The base image is pinned to one digest and the Python packages to exact
# versions (docker/requirements.txt), so a rebuild uses the same Python, the
# same system libraries and the same packages every time. Dependabot proposes
# a newer digest or version as a pull request.

FROM python:3.13-alpine3.24@sha256:c50ccfb66e5cc97a610dcd0b9597de6ef5b52c96280b51997b353a0fc87a8361 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY docker/requirements.txt ./
COPY daemon/pyproject.toml daemon/README.md daemon/LICENSE ./daemon/
COPY daemon/src ./daemon/src

# The pinned dependencies and then the daemon, into a virtual environment the
# final image copies. A wheel exists for this platform, so no compiler is
# needed, and pip itself is thrown away afterwards.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
 && /opt/venv/bin/pip install --no-cache-dir --no-deps ./daemon \
 && rm -rf /opt/venv/lib/python3.13/site-packages/pip* /opt/venv/bin/pip*


FROM python:3.13-alpine3.24@sha256:c50ccfb66e5cc97a610dcd0b9597de6ef5b52c96280b51997b353a0fc87a8361

# The licence the project is distributed under, and the notices for what it
# includes, travel with the image.
LABEL org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.title="Presto AI Dash" \
      org.opencontainers.image.source="https://github.com/jake1164/presto-ai-dash"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    DASH_DATABASE=/data/dash.db \
    DASH_HOST=0.0.0.0

# A non-root user owns the data directory. The daemon never needs to be root.
# The base image's own pip is removed too: nothing installs packages here.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip* /usr/local/bin/pip* \
 && addgroup -g 10001 dash \
 && adduser -D -u 10001 -G dash dash \
 && mkdir -p /data \
 && chown dash:dash /data

COPY --from=builder /opt/venv /opt/venv
COPY LICENSE THIRD_PARTY_NOTICES.md /licenses/

USER dash
WORKDIR /data

# The database and the encryption key file live here. A named volume keeps
# them across upgrades of the image.
VOLUME ["/data"]

# The device port and the admin page.
EXPOSE 8787 8788

# The health check asks one question: is the daemon alive? The answer is
# {"ok": true} and nothing else, and it needs no token.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3).read()"]

# The admin page listens on the loopback address unless DASH_ADMIN_HOST says
# otherwise. See
# https://jake1164.github.io/presto-ai-dash/getting-started/install-with-docker/
# (source: docs/user/getting-started/install-with-docker.md).
CMD ["python", "-m", "presto_ai_dash.app", "--admin"]
