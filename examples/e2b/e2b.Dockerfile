FROM e2bdev/code-interpreter:latest

RUN apt-get update && \
    apt-get install -y fuse libfuse2 && \
    rm -rf /var/lib/apt/lists/*
RUN pip install fusepy
RUN pip install nexus-ai-fs
