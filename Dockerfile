FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 결정론적 검증 엔진과 호스트 Docker 데몬을 통한 격리 PoC 실행에 필요한 도구.
# 이 이미지에서 Docker 데몬을 띄우지 않고 런타임에 마운트한 docker.sock만 사용한다.
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker-cli \
    git \
    universal-ctags \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저 설치(레이어 캐시)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 소스
COPY . .

EXPOSE 8000

# 대상 저장소는 볼륨 마운트(-v host:/repo -e REPO_PATH=/repo) 하거나
# REPO_URL 환경변수로 기동 시 clone 한다. API 키/DB 주소는 런타임에 -e 로 주입.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
