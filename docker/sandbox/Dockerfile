FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58
COPY --chmod=0555 app/infrastructure/sandbox_runner.py /opt/learningqa/runner.py
RUN mkdir -p /workspace && chown 65532:65532 /workspace && chmod 0700 /workspace
USER 65532:65532
WORKDIR /workspace
ENV HOME=/workspace LANG=C.UTF-8 LC_ALL=C.UTF-8 PATH=/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
ENTRYPOINT ["/usr/local/bin/python", "-I", "-B", "/opt/learningqa/runner.py"]
CMD []
