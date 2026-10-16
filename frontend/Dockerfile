FROM node:22-alpine AS build
WORKDIR /workspace
ARG FRONTEND_DIR=.
COPY ${FRONTEND_DIR}/package.json ${FRONTEND_DIR}/package-lock.json ./
RUN npm ci
COPY ${FRONTEND_DIR}/ .
RUN npm run build

FROM nginx:stable-alpine
ARG NGINX_CONFIG=nginx.conf
COPY ${NGINX_CONFIG} /etc/nginx/conf.d/default.conf
COPY --from=build /workspace/dist /usr/share/nginx/html
EXPOSE 80
