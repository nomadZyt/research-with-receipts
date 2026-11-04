# Playwright sandbox for Research with Receipts (Pattern B).
# The base image ships Chromium/Firefox/WebKit under /ms-playwright but not
# the Python package. Pin the package to the same version so `playwright install`
# is not run again inside the image.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /sandbox

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir "playwright==1.48.0"

COPY sandbox/worker.py /sandbox/worker.py

# Job I/O is expected via bind-mounted /job (input.json + out/)
RUN mkdir -p /job/out

# Hard timeout is enforced by the host (`docker run --stop-timeout` / runner kill).
CMD ["python", "worker.py"]
