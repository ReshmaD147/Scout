FROM node:20-slim AS build

WORKDIR /app/frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ .
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
