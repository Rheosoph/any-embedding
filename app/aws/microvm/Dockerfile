FROM ghcr.io/astral-sh/uv:0.12.8@sha256:d1cbaeadc234fe19c0d93daabcf5e98738cd93c6d1dd4918ef6aa30735feb23a AS uv

FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y \
        libgomp \
        python3.12 \
    && dnf clean all

WORKDIR /opt/any-embedding

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
ENV UV_PYTHON=/usr/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/any-embedding/.venv
RUN uv sync \
        --frozen \
        --no-dev \
        --no-install-project \
        --no-cache \
        --compile-bytecode

COPY download_model.py ./
ENV MODEL_PATH=/opt/model
RUN .venv/bin/python download_model.py

COPY server.py ./

ENV MODEL_ID=Alibaba-NLP/gte-multilingual-base \
    MODEL_API_NAME=gte-multilingual-base \
    EXPECTED_DIMENSIONS=768 \
    HF_HOME=/tmp/huggingface \
    HF_HUB_CACHE=/tmp/huggingface/hub \
    HF_MODULES_CACHE=/tmp/huggingface/modules \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=4 \
    OPENBLAS_NUM_THREADS=4 \
    MKL_NUM_THREADS=4 \
    TORCH_NUM_THREADS=4 \
    DYNAMIC_BATCH_WINDOW_MS=4 \
    MODEL_BATCH_SIZE=32 \
    JOB_TIMEOUT_SECONDS=840 \
    PORT=8080

EXPOSE 8080

CMD ["/opt/any-embedding/.venv/bin/python", "-u", "server.py"]
