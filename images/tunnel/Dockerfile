FROM alpine:3.22
RUN apk add --no-cache autossh openssh-client \
 && adduser -D -u 10001 tunnel
COPY entrypoint.sh /entrypoint.sh
USER 10001
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
