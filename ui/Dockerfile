# RAHBAR ui: static chat page on nginx, non-root, read-only root filesystem.
#   docker build -t rahbar-ui:dev ui
FROM nginxinc/nginx-unprivileged:1.27.3-alpine
USER root
RUN apk upgrade --no-cache
COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html
# uid 101 is the nginx user of the unprivileged image
USER 101
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
