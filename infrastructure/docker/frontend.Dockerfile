FROM node:24-alpine AS build
WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci
COPY frontend ./
RUN npm run build
FROM nginxinc/nginx-unprivileged:1.28-alpine
# A template, not a conf: the nginx entrypoint runs envsubst over /etc/nginx/templates so the
# request ceiling is rendered from the same environment value the application reads.
COPY infrastructure/docker/nginx.conf.template /etc/nginx/templates/default.conf.template
ENV MEDRAG_MAX_UPLOAD_MB=520
ENV MEDRAG_UPLOAD_TIMEOUT_SECONDS=1800
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
