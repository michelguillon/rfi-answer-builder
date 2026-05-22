# Dockerfile — frontend dev server (Vite + React + TS)
# -----------------------------------------------------
# ARCHITECTURAL DECISION: node:20-alpine, dev-only image.
#
# The frontend container's only job during the UI build phase is to
# run `vite` on port 3000 with hot reload. No production build, no
# nginx, no static bundle — those are deployment-time concerns we
# don't need for the internal-tool use case. Alpine keeps the image
# small and the npm install fast.
#
# The compose service bind-mounts the host's frontend/ over /app and
# uses an anonymous volume for /app/node_modules (see docker-compose
# .yml) so:
#   - host edits to .tsx/.ts files propagate instantly via Vite HMR
#   - the container's node_modules (built once, here) is NOT shadowed
#     by the host's potentially-absent or wrong-platform node_modules
FROM node:20-alpine

WORKDIR /app

# Copy ONLY package files first so the npm install layer caches when
# source changes but deps don't.
COPY package.json package-lock.json* ./
RUN npm install

# Source comes in via the bind-mount at runtime. The COPY here is
# only for the case where the image is run without the compose mount
# (e.g. a one-off `docker build` to smoke-test).
COPY . .

EXPOSE 3000
CMD ["npx", "vite", "--host", "0.0.0.0", "--port", "3000"]
