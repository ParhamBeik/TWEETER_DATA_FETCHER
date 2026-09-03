# Multi-stage: build the Vite SPA, then serve dist via nginx.
# Build context is the repo root.
# node 24 to match .github/workflows/ci.yml. The image only runs `vite
# build`, which works on older majors, but building on a version CI never
# exercises is how a 'works in CI, breaks in the image' failure appears.
FROM node:24-alpine AS build

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine

RUN rm /etc/nginx/conf.d/default.conf
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
