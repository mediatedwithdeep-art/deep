# ── build ────────────────────────────────────────────────────────────
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# Empty VITE_API_URL means same-origin requests, which nginx proxies. That
# is what makes the container work behind any hostname without a rebuild.
ENV VITE_API_URL=""
RUN npm run build

# ── serve ────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD wget -qO- http://127.0.0.1/index.html >/dev/null || exit 1
