# syntax=docker/dockerfile:1.7

ARG PANEL_REF=v1.1.18

# ------------------------------------------------------------
# Build Zomboid Control Panel at an explicit Git tag.
# ------------------------------------------------------------
FROM node:22-bookworm-slim AS panel-build

ARG PANEL_REF

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       git \
    && rm -rf /var/lib/apt/lists/*

RUN git clone \
      --depth 1 \
      --branch "${PANEL_REF}" \
      https://github.com/fpsacha/zomboid-control-panel.git \
      /src

WORKDIR /src

# The upstream Dockerfile uses npm install rather than assuming
# an absolutely immutable lockfile across optional architectures.
RUN cd client \
    && npm install --no-audit --include=optional \
    && npm run build

RUN npm install --no-audit --omit=dev

# ------------------------------------------------------------
# Runtime: same SteamCMD base family as upstream all-in-one.
# ------------------------------------------------------------
FROM cm2network/steamcmd:root

USER root

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       curl \
       gnupg \
       iproute2 \
       lib32gcc-s1 \
       lib32stdc++6 \
       procps \
       tini \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=panel-build --chown=steam:steam /src/package.json /src/package-lock.json* ./
COPY --from=panel-build --chown=steam:steam /src/node_modules ./node_modules
COPY --from=panel-build --chown=steam:steam /src/server ./server
COPY --from=panel-build --chown=steam:steam /src/client/dist ./client/dist
COPY --from=panel-build --chown=steam:steam /src/pz-mod ./pz-mod
COPY --from=panel-build --chown=steam:steam /src/browser-extension ./browser-extension

COPY entrypoint.sh /entrypoint.sh

RUN chmod 0755 /entrypoint.sh \
    && mkdir -p \
       /pz-server \
       /zomboid \
       /app/data \
       /app/logs \
       /home/steam/Steam \
    && chown -R steam:steam \
       /pz-server \
       /zomboid \
       /app \
       /home/steam/Steam

ENV NODE_ENV=production \
    PORT=3001 \
    PZ_SERVER_PATH=/pz-server \
    PZ_SAVE_PATH=/zomboid \
    PZ_SERVER_BAT=start-server.sh \
    RCON_HOST=127.0.0.1 \
    RCON_PORT=27015

EXPOSE 3001/tcp 16261/udp 16262/udp

HEALTHCHECK \
    --interval=30s \
    --timeout=5s \
    --start-period=15m \
    --retries=5 \
    CMD node -e "require('http').get('http://127.0.0.1:3001/api/health',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"

ENTRYPOINT ["/entrypoint.sh"]
