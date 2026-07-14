# 本地数据服务镜像
# 构建上下文为工程根目录（见 docker-compose.yml 的 context: ..），
# 以便将 service/ 与 skills/*/scripts/ 一同打入镜像供 loader 自动发现。
FROM python:3.11-slim

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY service/requirements.txt ./service/requirements.txt
RUN pip install --no-cache-dir -r service/requirements.txt

# 服务框架 + 各技能脚本（功能模块）+ Web 前端（同源部署）
COPY service ./service
COPY skills ./skills
COPY web ./web

RUN mkdir -p /app/cache /app/data

EXPOSE 18901
WORKDIR /app/service
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18901"]
