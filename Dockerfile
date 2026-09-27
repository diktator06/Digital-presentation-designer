# --- сборка UI -----------------------------------------------------------------
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- сервис ---------------------------------------------------------------------
FROM python:3.12-slim
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 DECKSMITH_SOFFICE=/usr/bin/soffice
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-impress libreoffice-core fonts-dejavu fonts-liberation fonts-crosextra-carlito \
      fontconfig curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# открытые шрифты, которые используют шаблоны датасета (замены Google Slides)
RUN mkdir -p /usr/share/fonts/truetype/google && cd /usr/share/fonts/truetype/google \
 && curl -fsSLO https://github.com/google/fonts/raw/main/ofl/play/Play-Regular.ttf \
 && curl -fsSLO https://github.com/google/fonts/raw/main/ofl/play/Play-Bold.ttf \
 && curl -fsSL -o Montserrat.ttf "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf" \
 && fc-cache -f
WORKDIR /app
COPY pyproject.toml README.md ./
COPY decksmith/ decksmith/
RUN pip install --no-cache-dir -e .
COPY skills/ skills/
COPY config/ config/
COPY configs/ configs/
COPY assets/ assets/
COPY data/ data/
COPY --from=ui /ui/dist frontend/dist
EXPOSE 8000
CMD ["decksmith", "serve", "--port", "8000"]
