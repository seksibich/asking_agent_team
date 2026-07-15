# 数据服务镜像（前后端同源 + DB）
# 构建上下文为仓库根目录（含 service/ 后端与 service/web 前端、agent/skills/*/scripts 功能脚本）。
FROM python:3.11-slim

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY profile/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 后端框架 + Web 前端（service/web，同源部署）+ 各技能脚本（功能模块，位于 agent/skills/*/scripts）
COPY service ./service
COPY agent/skills ./agent/skills

RUN mkdir -p /app/cache /app/data

EXPOSE 18901
WORKDIR /app/service
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18901"]
