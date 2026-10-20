FROM alpine:3.20

RUN apk add --no-cache ca-certificates

# Railway builds with the repository root as Docker context.
COPY ops/football_wake/wake_and_verify.sh /usr/local/bin/football-wake
RUN sed -i 's/\r$//' /usr/local/bin/football-wake \
    && chmod +x /usr/local/bin/football-wake

ENTRYPOINT ["/usr/local/bin/football-wake"]
