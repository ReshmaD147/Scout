FROM node:20-slim AS build

WORKDIR /app/frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ .

# Vite bakes VITE_ variables into the build at build time - Railway's
# service Variables are only injected at container RUNTIME, not during
# the Docker build step. Confirmed via a real deployment: the variable
# existed correctly in Railway's config, but the built JS still had the
# localhost fallback baked in, since npm run build never saw it. Fixed
# by explicitly accepting it as a build ARG and setting it as an ENV
# for the build step specifically.
ARG VITE_API_BASE_URL
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ARG VITE_STRIPE_PUBLISHABLE_KEY
ENV VITE_STRIPE_PUBLISHABLE_KEY=$VITE_STRIPE_PUBLISHABLE_KEY

RUN npm run build

FROM nginx:alpine

COPY --from=build /app/frontend/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

# nginx's official image auto-substitutes environment variables into
# any file in /etc/nginx/templates/ at container startup, writing the
# result to /etc/nginx/conf.d/ - this is how we make nginx listen on
# Railway's dynamically-assigned $PORT instead of a hardcoded port,
# which caused a real, confirmed 502 error when deployed (Railway's
# proxy expects the app to listen on $PORT, not a fixed port 80).
ENV PORT=80

EXPOSE $PORT

CMD ["nginx", "-g", "daemon off;"]
