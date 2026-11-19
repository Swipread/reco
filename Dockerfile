# Production image of the recommender: one image, two processes chosen by the command
# (`api` or `worker`). CPU-only torch, embedding model baked in, non-root user.
# The tests do not run here (CI): docker build -t swipread-reco .

FROM python:3.12.16-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# Dependencies in their own layer: changing the code does not download them again
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable
# The model at the revision the encoder loads (worker/encode.py), only the files it needs.
# Plain HTTP download: the default Xet transfer can fail or hang (see docs/faq.md).
ENV HF_HOME=/opt/hf
RUN HF_HUB_DISABLE_XET=1 /opt/venv/bin/python -c "\
from huggingface_hub import snapshot_download; \
from swipread_reco.worker.encode import HF_FILES, HF_MODEL, HF_REVISION; \
snapshot_download(HF_MODEL, revision=HF_REVISION, allow_patterns=HF_FILES)" \
    && chmod -R a+rX /opt/hf

FROM python:3.12.16-slim-trixie
RUN groupadd --system swipread && useradd --system --gid swipread --no-create-home swipread
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/hf /opt/hf
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    TRANSFORMERS_OFFLINE=1 \
    RECO_HOST=0.0.0.0 \
    RECO_PORT=8081
USER swipread
EXPOSE 8081
ENTRYPOINT ["python", "-m", "swipread_reco"]
CMD ["api"]
