# Slice 1: Vite dev server in a container (a production nginx build comes later).
FROM node:24-alpine

WORKDIR /app

# Install deps first for layer caching; only re-run when the lockfile changes.
COPY package.json package-lock.json* ./
RUN npm ci

COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
