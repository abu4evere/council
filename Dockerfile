# Unstuck, as a container. Built for Fly.io but plain enough to run anywhere
# that takes a Dockerfile.
#
# Python 3.12 rather than 3.14: every dependency here ships prebuilt wheels for
# it, so the image builds in seconds instead of compiling cryptography from
# source. Nothing in the codebase needs a newer version.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The volume is mounted at /data. This must NOT sit under /app: that directory
# is part of the image and is replaced wholesale on every deploy, which would
# delete every account and conversation each time you shipped a change.
ENV COUNCIL_DB=/data/council.db

EXPOSE 8080
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
