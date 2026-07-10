FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 결정론적 검증 엔진에 필요한 시스템 도구: git(커밋/이력 조회), universal-ctags(심볼 인덱싱)
RUN apt-get update && apt-get install -y --no-install-recommends \
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
