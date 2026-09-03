FROM node:22-alpine

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY --chown=node:node . .
RUN npm run build \
    && mkdir -p node_modules/.vite-temp \
    && chown node:node node_modules/.vite-temp

USER node

EXPOSE 5173
CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "5173"]
