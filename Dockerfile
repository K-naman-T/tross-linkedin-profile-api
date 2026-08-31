FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Provide at runtime:
#   -e API_KEY=...            (required)
#   -e LINKEDIN_COOKIES=...    (full linkedin.com cookie jar as JSON; required)
# cookies.json in the build context also works.
EXPOSE 8099
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8099"]
