# =============================================================================
# Kuantix 后端容器镜像
#
# 目标平台：腾讯云开发 CloudBase —— 云托管 CloudRun「容器模式」
# 目标架构：linux/amd64（云托管运行在 x86_64 节点，Apple Silicon 本地构建
#           务必加 --platform linux/amd64，否则推上去会因架构不符启动失败）
#
# 设计要点（改动前请先读完这段注释）：
#   1) 基础镜像固定 python:3.12-slim
#      - 项目要求 Python >= 3.10（pyproject.toml:10）
#      - 选 3.11+ 可用标准库 tomllib，不会额外拉进 tomli
#      - slim 而非 alpine：pandas / pyarrow / scipy 在 musl 上没有官方 wheel，
#        用 alpine 会退化成源码编译，构建时间从分钟级涨到小时级
#
#   2) 依赖安装必须是 `pip install --no-cache-dir .`
#      - 不带 [dev]：pytest / pytest-cov / ruff 不进生产镜像
#      - 不加 -U / --upgrade：pyproject.toml:16-20 有两条版本红线
#          * easy-tdx 精确锁 1.20.3（漂移会破坏 _SECURITY_COEFFICIENTS 引用，NF-25）
#          * pandas < 3（3.x 会破坏 easy-tdx 的 DataFrame 构造路径）
#        任何形式的升级安装都会打破这两条锁。
#
#   3) 【重要】这里刻意不创建数据目录（不写 RUN mkdir -p /mnt/Kuantix/...）
#      如果在云托管上挂载了 CFS 文件存储，挂载动作会覆盖挂载点的原有内容，
#      镜像里 mkdir 出来的目录在挂载后会消失。
#      容器启动顺序是：挂载卷 -> 执行 ENTRYPOINT，
#      所以数据子目录统一由 docker-entrypoint.sh 在启动时创建。
#
#   4) 时区固定 Asia/Shanghai
#      APScheduler 的盘后 cron 用的是 Asia/Shanghai（scheduler.py:94），
#      代码内部处理正确；但容器默认 UTC 会让日志时间戳比北京时间慢 8 小时，
#      排查问题时极易看混，所以在镜像层就把系统时区settle掉。
# =============================================================================

FROM python:3.12-slim

# -----------------------------------------------------------------------------
# 环境变量
#   TZ                : 系统时区
#   PYTHONUNBUFFERED  : 关掉 stdout 缓冲，日志才能实时出现在云托管的日志面板
#   PYTHONDONTWRITEBYTECODE : 不生成 .pyc，减小镜像体积
#   Kuantix_CONFIG     : 显式指定配置文件位置，避免依赖「当前工作目录」的搜索顺序
#   Kuantix_DATA_ROOT  : 数据根目录，docker-entrypoint.sh 会据此派生 7 个路径变量
#                       CFS 的默认挂载路径是 /mnt，所以数据根定在 /mnt/Kuantix
# -----------------------------------------------------------------------------
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    Kuantix_CONFIG=/app/config.toml \
    Kuantix_DATA_ROOT=/mnt/Kuantix

# -----------------------------------------------------------------------------
# 系统依赖
#   tzdata          : 时区数据库（没有它 TZ 变量不生效）
#   ca-certificates : HTTPS 根证书
# 装完立刻清 apt 缓存，避免残留在镜像层里。
# -----------------------------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tzdata \
        ca-certificates \
    && ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime \
    && echo "${TZ}" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# -----------------------------------------------------------------------------
# 拷贝构建所需文件
#
# 这三个文件缺一不可：
#   pyproject.toml : 依赖声明 + 构建配置
#   README.md      : pyproject.toml:9 声明了 readme = "README.md"，缺失会构建失败
#   config.toml    : pyproject.toml:52-53 的 force-include 会把它打包成
#                    Kuantix/resources/config.default.toml，缺失会构建失败
# -----------------------------------------------------------------------------
COPY pyproject.toml README.md config.toml ./
COPY Kuantix ./Kuantix

# -----------------------------------------------------------------------------
# 安装 Kuantix 及其运行时依赖
# 这一层比较慢（pyarrow 126MB + scipy 99MB + pandas 70MB + numpy 28MB），
# 放在源码 COPY 之后是为了让前面的层能被缓存复用。
#
# 【构建后端与 --no-build-isolation】
#   pyproject.toml 的 [build-system] 声明 requires = ["hatchling>=1.21"]。
#   默认 `pip install .` 会新建一个隔离环境去装构建后端，并在里面解析
#   hatchling 的依赖（trove-classifiers 等）。在慢网环境下这一步极易触发
#   依赖回溯失败（ResolutionImpossible: hatchling depends on trove-classifiers），
#   导致构建失败，且报错与项目本身无关。
#   解决办法：先在基础镜像里显式装好构建后端，再用 --no-build-isolation 复用
#   它们、跳过隔离解析。这是社区通用的稳妥写法，不会改动项目的构建配置。
#
# 【config.default.toml 单一事实来源（P0 修复）】
#   源码树中不再物理维护 Kuantix/resources/config.default.toml；
#   pyproject.toml 的 force-include 会在打包时把根目录 config.toml
#   自动注入为 wheel 内的 Kuantix/resources/config.default.toml。
#   这样保证「根 config.toml 是唯一事实来源」，且避免 wheel 构建
#   时同名路径被写两次导致的 hatchling ValueError。
# -----------------------------------------------------------------------------
RUN pip install --no-cache-dir "hatchling>=1.21" "trove-classifiers" \
    && pip install --no-cache-dir --no-build-isolation .

# -----------------------------------------------------------------------------
# 启动脚本
# 单独放在最后一层：改脚本时不会让上面的依赖层缓存失效。
# 用 sed 去掉可能存在的 Windows 换行符（\r），否则 Linux 下会报
# "exec format error" 或 "no such file or directory"。
# -----------------------------------------------------------------------------
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

# 默认监听端口。云托管若注入 PORT 环境变量，entrypoint 会优先采用 PORT。
EXPOSE 8899

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
