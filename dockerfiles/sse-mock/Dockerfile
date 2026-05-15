FROM python:3.13-slim

RUN pip install --no-cache-dir aiohttp

WORKDIR /app
COPY server.py /app/server.py

EXPOSE 8080
CMD ["python", "/app/server.py"]
