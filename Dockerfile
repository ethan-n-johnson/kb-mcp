# kb-mcp image. Build for the EC2 instances with:
#   docker build --platform linux/amd64 -t kb-mcp:dev .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, for layer caching. At this point the kb package is not
# copied yet, so this installs only the dependencies listed in pyproject.toml.
COPY pyproject.toml ./
RUN pip install .

# Application code, then install the package itself over the dependency layer.
COPY kb/ ./kb/
RUN pip install --no-deps --force-reinstall . \
    && rm -rf build ./*.egg-info

# Seed corpus files (phase 11). seeds/.keep keeps the directory in the build context.
COPY seeds/ ./seeds/

# Non-root runtime user. /models is filled below as this user, so no chown layer
# duplicates the model files. /data holds the SQLite database; on the instance it is a
# host directory bind mounted here and snapshotted to S3 (kb/snapshot.py).
RUN groupadd --gid 10001 kb \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin kb \
    && mkdir -p /models /data \
    && chown 10001:10001 /models /data

USER 10001:10001

ENV KB_MODEL_DIR=/models \
    FASTEMBED_CACHE_PATH=/models

# Download both ONNX models into /models during the build and run one tiny
# inference each, so a broken download fails the build instead of the first request.
RUN python -c "import os; \
from fastembed import TextEmbedding; \
from fastembed.rerank.cross_encoder import TextCrossEncoder; \
d = os.environ['KB_MODEL_DIR']; \
e = TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir=d); \
c = TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2', cache_dir=d); \
print('embedding dim', len(next(iter(e.embed(['warm up']))))); \
print('rerank score', list(c.rerank('warm up', ['warm up']))[0])"

# Only after the download: the models must now load from the local cache, never the network.
ENV HF_HUB_OFFLINE=1

EXPOSE 8000

# uvicorn's own proxy handling is off: with --forwarded-allow-ips '*' it trusts the
# left-most X-Forwarded-For entry, which the caller chooses. kb.clientip counts from
# the right instead (KB_PROXY_HOPS, set in the instance user data).
CMD ["uvicorn", "kb.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
