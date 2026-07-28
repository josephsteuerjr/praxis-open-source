FROM rust:1.85-slim-bookworm AS hands-builder
WORKDIR /build
COPY hands/Cargo.toml hands/Cargo.lock ./
COPY hands/src ./src
RUN cargo test --release --locked && cargo build --release --locked

FROM python:3.12-slim
WORKDIR /app
# Руки, которыми она делает картинку. Отдельного тула под это НЕТ и не нужно: у неё есть
# свой shell в этом контейнере и send_file — значит она пишет скрипт и рисует сама.
#   libcairo2                — системная половина cairosvg (SVG → PNG);
#   chromium                 — /usr/bin/chromium, рендер HTML-страницы в снимок
#                              (запускать с --no-sandbox: мы root). Переменной с путём
#                              НЕ заводим: её никто не читает, а манифест не должен
#                              называть ей рычаги, которых нет.
#   fonts-dejavu/liberation  — иначе кириллица в графиках и снимках станет квадратами.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git libcairo2 chromium fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && git config --global --add safe.directory /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=hands-builder /build/target/release/praxis-hands /usr/local/bin/praxis-hands
ENV PRAXIS_HANDS=/usr/local/bin/praxis-hands
CMD ["python", "bootguard.py"]
