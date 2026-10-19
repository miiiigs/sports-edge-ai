FROM alpine:3.20

RUN apk add --no-cache ca-certificates

COPY wake_and_verify.sh /usr/local/bin/football-wake
RUN sed -i 's/\r$//' /usr/local/bin/football-wake \
    && chmod +x /usr/local/bin/football-wake

ENTRYPOINT ["/usr/local/bin/football-wake"]
