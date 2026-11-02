# Minimal Playwright sandbox for Research with Receipts (Pattern B)
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /sandbox

COPY sandbox/worker.py /sandbox/worker.py

# Job I/O is expected via bind-mounted /job (input.json + out/)
RUN mkdir -p /job/out

# Hard timeout is enforced by the host (`docker run --stop-timeout` / runner kill).
CMD ["python", "worker.py"]
