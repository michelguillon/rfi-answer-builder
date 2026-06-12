# Dockerfile — RAG pipeline runtime
# ---------------------------------
# DECISION: python:3.13-slim base.
#   "slim" drops build toolchains and docs we don't need — smaller image,
#   faster pulls. The full image only pays off when compiling C extensions;
#   our dependencies ship prebuilt wheels, so slim is sufficient.
FROM python:3.13-slim

WORKDIR /app

# DECISION: copy requirements.txt FIRST, install, THEN copy source.
#   Docker caches each layer. Source changes far more often than the
#   dependency list — putting the (slow) pip install before the source
#   copy means editing a .py file does NOT trigger a reinstall.
COPY requirements.txt .

# DECISION: install CPU-only torch from the PyTorch CPU index BEFORE
# the main requirements pass.
#   sentence-transformers (our cross-encoder reranker) depends on torch.
#   From the default PyPI index, torch resolves to the CUDA build, which
#   drags in ~2GB of nvidia_* wheels (cuSOLVER, cuBLAS, cuDNN, ...). The
#   deployment target is a GPU-less Lenovo M720q, so every one of those
#   bytes is dead weight: it bloats the image and turns a clean rebuild
#   into a 5-minute, 2GB download. Installing the CPU wheel first pins
#   torch, so the subsequent `-r requirements.txt` sees the requirement
#   already satisfied and never pulls the CUDA variant. Inference is
#   CPU-only either way — this changes build cost, not runtime behaviour.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project. .dockerignore keeps .env, .git, the
# ChromaDB store, and the data/outputs volumes OUT of the image —
# those are mounted at runtime, and .env must never be baked in.
COPY . .
