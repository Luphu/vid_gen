FROM python:3.13-alpine
RUN pip install --no-cache-dir "requests>=2.31,<3" "openpyxl>=3.1,<4" tzdata \
 && adduser -D -u 10001 report
COPY report.py /app/report.py
USER 10001
ENTRYPOINT ["python3", "/app/report.py"]
