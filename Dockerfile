# 数据服务镜像（前后端同源 + DB）
# 构建上下文为仓库根目录（含 service/ 后端、skills/ 功能脚本、web/ 前端）。
FROM python:3.11-slim

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 后端框架 + 各技能脚本（功能模块）+ Web 前端（同源部署）
COPY service ./service
COPY skills ./skills
COPY web ./web

RUN mkdir -p /app/cache /app/data

EXPOSE 18901
WORKDIR /app/service
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18901"]
