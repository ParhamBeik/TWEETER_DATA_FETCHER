# Multi-stage: build the Vite SPA, then serve dist via nginx.
# One origin: nginx serves the SPA root and reverse-proxies /api/ and /admin/
# to the Django `web` service, so the browser never talks to :8000 directly.
FROM node:20-alpine AS build

WORKDIR /app

# Install deps first for layer caching. package-lock.json is committed.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine

# Replace the default site config so /api and /admin go to the backend service.
RUN rm /etc/nginx/conf.d/default.conf
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
