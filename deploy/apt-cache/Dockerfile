FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends apt-cacher-ng ca-certificates \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 3142

CMD ["/bin/sh", "-c", "chown -R apt-cacher-ng:apt-cacher-ng /var/cache/apt-cacher-ng /var/log/apt-cacher-ng && exec /usr/sbin/apt-cacher-ng -c /etc/apt-cacher-ng ForeGround=1 BindAddress=0.0.0.0"]
