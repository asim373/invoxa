FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY --chown=node:node . .
ARG VITE_API_BASE_URL
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ARG VITE_PUBLIC_APP_URL
ENV VITE_PUBLIC_APP_URL=$VITE_PUBLIC_APP_URL
ARG VITE_BUILD_MODE=production
ENV VITE_BUILD_MODE=$VITE_BUILD_MODE
RUN npm run build -- --mode $VITE_BUILD_MODE

FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080
