# Multi-stage: build the Vite SPA, then serve dist via nginx.
# Build context is the git repo root.
FROM node:20-alpine AS build

WORKDIR /app

COPY twitter-saas/frontend/package.json twitter-saas/frontend/package-lock.json ./
RUN npm ci

COPY twitter-saas/frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine

RUN rm /etc/nginx/conf.d/default.conf
COPY twitter-saas/frontend/nginx.conf /etc/nginx/conf.d/default.conf

COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
