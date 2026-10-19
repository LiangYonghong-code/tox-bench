# 基础镜像：只装数据与依赖，**不装判分器、不装参考解**。
# 构建上下文就是 environment/ 目录，因此 build-tools/ 与 tests/ 天然不会进入镜像。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 固定版本以保证可复现；与 repeat-dose-noael-judgment 用同一组版本，
# 让两题的运行环境一致、镜像层可共享。
RUN pip install --no-cache-dir \
        numpy==1.26.4 \
        pandas==2.2.2 \
        scipy==1.13.1 \
        pytest==8.2.2 \
        pytest-json-ctrf==0.3.5

WORKDIR /app

# 只复制数据。建库/金标准脚本刻意不进镜像（它们住在任务目录之外的
# build-tools/clinical-tk-parameter-analysis/），避免泄漏真值模型参数与标准解。
COPY study.db /app/study.db

CMD ["sleep", "infinity"]
