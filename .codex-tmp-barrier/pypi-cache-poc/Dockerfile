FROM python:3.13-slim-bookworm

RUN python -m pip install --no-cache-dir devpi-server==6.20.0

COPY start-devpi.sh /usr/local/bin/start-devpi
RUN chmod 0755 /usr/local/bin/start-devpi

EXPOSE 3141
ENTRYPOINT ["/usr/local/bin/start-devpi"]
