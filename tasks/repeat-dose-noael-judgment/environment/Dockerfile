# 基础镜像：只装数据与依赖，**不装判分器、不装参考解**。
# 构建上下文就是 environment/ 目录，因此 build-tools/ 与 tests/ 天然不会进入镜像。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 固定版本以保证可复现；pandas/numpy/scipy 是专业领域数据分析的常用库
RUN pip install --no-cache-dir \
        numpy==1.26.4 \
        pandas==2.2.2 \
        scipy==1.13.1 \
        pytest==8.2.2 \
        pytest-json-ctrf==0.3.5

WORKDIR /app

COPY study.db /app/study.db
COPY SOP_toxicity_criteria.md /app/SOP_toxicity_criteria.md

CMD ["sleep", "infinity"]
