FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY sab_watchdog.py .


RUN pip install requests

CMD ["python", "sab_watchdog.py"]

