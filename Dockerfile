ARG HERMES_IMAGE=nousresearch/hermes-agent:v2026.7.20
FROM ${HERMES_IMAGE}

ARG INSTALL_LOCAL_STT=true
USER root

RUN uv pip install \
      --python /opt/hermes/.venv/bin/python \
      "ddgs==9.14.4" \
      "edge-tts==7.2.7" && \
    if [ "${INSTALL_LOCAL_STT}" = "true" ]; then \
      uv pip install \
        --python /opt/hermes/.venv/bin/python \
        "faster-whisper==1.2.1" \
        "numpy==2.4.3"; \
    fi

RUN printf '%s\n' \
      'export PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:$PATH"' \
      > /etc/profile.d/hermes-venv.sh && \
    chmod 644 /etc/profile.d/hermes-venv.sh && \
    /bin/bash -lc \
      'python -c "import googleapiclient, google_auth_oauthlib"'

LABEL org.opencontainers.image.title="Hermes Personal Assistant"
LABEL org.opencontainers.image.description="Hermes Agent with Russian voice and Google Workspace support"
