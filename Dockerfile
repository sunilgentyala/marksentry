FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY marksentry ./marksentry
RUN pip install --no-cache-dir .

RUN useradd --uid 1000 --create-home marksentry
USER marksentry
WORKDIR /work

ENTRYPOINT ["marksentry"]
CMD ["--help"]
