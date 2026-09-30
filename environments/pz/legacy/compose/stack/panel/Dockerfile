FROM node:22-bookworm-slim AS build
ARG PANEL_REF=v1.3.7
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${PANEL_REF}" https://github.com/fpsacha/zomboid-control-panel.git /app
WORKDIR /app
RUN cd client && npm install --no-audit --include=optional && npm run build
RUN npm install --no-audit --omit=dev

FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl procps && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build --chown=node:node /app/package.json /app/package-lock.json ./
COPY --from=build --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/server ./server
COPY --from=build --chown=node:node /app/client/dist ./client/dist
COPY --from=build --chown=node:node /app/pz-mod ./pz-mod
RUN mkdir -p /app/data /app/logs && chown node:node /app/data /app/logs
USER node
ENV NODE_ENV=production PORT=3001
EXPOSE 3001
CMD ["node", "server/index.js"]
