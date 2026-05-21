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
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project. .dockerignore keeps .env, .git, the
# ChromaDB store, and the data/outputs volumes OUT of the image —
# those are mounted at runtime, and .env must never be baked in.
COPY . .
