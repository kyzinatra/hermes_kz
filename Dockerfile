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

COPY scripts/hermes_korea_gateway.py /opt/hermes/bin/hermes_korea_gateway.py
COPY scripts/gateway_egress_proxy.py /opt/hermes/bin/gateway_egress_proxy.py
COPY scripts/hermes_yandex_mail.py /opt/hermes/bin/hermes_yandex_mail.py
# The production gateway is owned by Docker CMD.  Replacing upstream profile
# reconciliation prevents persisted state from launching a second poller.
COPY scripts/hermes_single_gateway_init.sh /etc/cont-init.d/02-reconcile-profiles
RUN sed -i 's/\r$//' /etc/cont-init.d/02-reconcile-profiles && \
    chmod 755 \
      /opt/hermes/bin/hermes_korea_gateway.py \
      /opt/hermes/bin/gateway_egress_proxy.py \
      /opt/hermes/bin/hermes_yandex_mail.py \
      /etc/cont-init.d/02-reconcile-profiles && \
    /opt/hermes/.venv/bin/python -m py_compile \
      /opt/hermes/bin/hermes_korea_gateway.py \
      /opt/hermes/bin/gateway_egress_proxy.py \
      /opt/hermes/bin/hermes_yandex_mail.py

# Explicit administrative commands still override this default normally.
CMD ["/opt/hermes/.venv/bin/python", "/opt/hermes/bin/hermes_korea_gateway.py", "gateway", "run", "--no-supervise", "--external-supervisor"]

LABEL org.opencontainers.image.title="Hermes Personal Assistant"
LABEL org.opencontainers.image.description="Hermes Agent with Russian voice, cost-aware web search, Korea maps, and Google Workspace"
