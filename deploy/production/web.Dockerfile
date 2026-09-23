FROM nginx:stable-alpine@sha256:ef8676b33d681f272ba429b27658bdd7e640963279714c96bddf1dc76307f7b6
COPY frontend-static /usr/share/nginx/html
COPY deploy/production/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8443
