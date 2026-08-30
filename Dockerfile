ARG HERMES_IMAGE=nousresearch/hermes-agent:v2026.8.27@sha256:e0df6adebddf29b91112aefc999d4aaf6846c9eb544faca5672a16a13590ff79
FROM ${HERMES_IMAGE}

ARG INSTALL_LOCAL_STT=true
ARG TARGETARCH
USER root

COPY requirements/*.lock /opt/hermes/requirements/

# Every direct and transitive package is pinned and hash-verified.  The lock
# files are resolved separately for the base image's supported Linux
# architectures and keep its already-tested dependency versions.
RUN case "${TARGETARCH}" in amd64|arm64) ;; *) \
      echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 2 ;; esac && \
    uv pip install \
      --no-config \
      --python /opt/hermes/.venv/bin/python \
      --require-hashes \
      --strict \
      --requirements "/opt/hermes/requirements/core-${TARGETARCH}.lock" && \
    if [ "${INSTALL_LOCAL_STT}" = "true" ]; then \
      uv pip install \
        --no-config \
        --python /opt/hermes/.venv/bin/python \
        --require-hashes \
        --strict \
        --requirements "/opt/hermes/requirements/stt-${TARGETARCH}.lock"; \
    fi

RUN printf '%s\n' \
      'export PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:$PATH"' \
      > /etc/profile.d/hermes-venv.sh && \
    chmod 644 /etc/profile.d/hermes-venv.sh && \
    /bin/bash -lc \
      'python -c "import googleapiclient, google_auth_oauthlib"'

# The normal Hermes plugin loader is intentionally non-fatal.  Telegram
# coordinates need a stronger boundary, so the production gateway starts via
# a launcher that lets Hermes resolve its active profile, then installs and
# asserts the Korea ingress guard before CLI dispatch starts any adapter.
COPY scripts/hermes_korea_gateway.py /opt/hermes/bin/hermes_korea_gateway.py
COPY scripts/gateway_egress_proxy.py /opt/hermes/bin/gateway_egress_proxy.py
COPY scripts/hermes_yandex_mail.py /opt/hermes/bin/hermes_yandex_mail.py
RUN chmod 755 \
      /opt/hermes/bin/hermes_korea_gateway.py \
      /opt/hermes/bin/gateway_egress_proxy.py \
      /opt/hermes/bin/hermes_yandex_mail.py && \
    /opt/hermes/.venv/bin/python -m py_compile \
      /opt/hermes/bin/hermes_korea_gateway.py \
      /opt/hermes/bin/gateway_egress_proxy.py \
      /opt/hermes/bin/hermes_yandex_mail.py

# Make the guarded gateway the image-level default as well as the Compose
# default.  Explicit administrative commands still override CMD normally.
CMD ["/opt/hermes/.venv/bin/python", "/opt/hermes/bin/hermes_korea_gateway.py", "gateway", "run", "--no-supervise", "--external-supervisor"]

LABEL org.opencontainers.image.title="Hermes Personal Assistant"
LABEL org.opencontainers.image.description="Hermes Agent with Russian voice, cost-aware web search, Korea maps, and Google Workspace"
