#!/bin/sh
# =============================================================================
# Kuantix 容器启动脚本（腾讯云开发 CloudRun 容器模式）
#
# 这个脚本存在的唯一目的：在不改动任何业务代码的前提下，
# 把「云平台的约定」翻译成「Kuantix 配置系统认识的约定」。
#
# 需要翻译的有两件事：
#
# 【1】端口
#     云平台注入的环境变量叫 PORT。
#     但 Kuantix 的配置系统（Kuantix/config.py:466-508）只认恰好两段的
#     Kuantix__<SECTION>__<KEY> 格式，直接给一个 PORT 它根本不看。
#     更糟的是 _apply_env_overrides 会遍历所有 Kuantix__ 开头的变量，
#     一旦指向不存在的分节或键，会直接抛 MissingConfigError 让服务起不来
#     （这是项目刻意设计的 fail-loud 红线 NF-26，不是 bug）。
#     所以这里把 PORT 显式翻译成 Kuantix__SERVER__PORT。
#
# 【2】数据目录
#     仅设置 Kuantix__PATHS__ROOT 即可（P0 修复：PathsConfig 支持从 root 自动派生
#     vipdoc/factors/db/logs/reports/exports）。如需把某个子目录挂到独立存储
#     （如 db 到独立 SSD），再单独覆写对应的 Kuantix__PATHS__DB 等变量。
#     未覆盖时，7 个路径默认派生到 /mnt/Kuantix/<name>。
#
# 【3】目录创建为什么放在这里而不是 Dockerfile
#     如果挂载了 CFS 文件存储，挂载动作会覆盖挂载点的原有内容。
#     容器启动顺序是「先挂载卷，再执行本脚本」，
#     所以只有在本脚本里创建的子目录才能真正落在 CFS 上并保留下来。
# =============================================================================

set -e

log() {
    echo "[Kuantix-entrypoint] $*"
}

log "===================== Kuantix 容器启动 ====================="
log "时区       : ${TZ:-未设置}  (当前时间 $(date '+%Y-%m-%d %H:%M:%S %Z'))"

# -----------------------------------------------------------------------------
# 1. 配置文件位置
#    显式指定，避免依赖 config.py:517-548 的「当前工作目录」搜索顺序。
# -----------------------------------------------------------------------------
export Kuantix_CONFIG="${Kuantix_CONFIG:-/app/config.toml}"

if [ ! -f "${Kuantix_CONFIG}" ]; then
    log "错误：找不到配置文件 ${Kuantix_CONFIG}"
    log "      镜像构建时应已把项目根目录的 config.toml 拷到 /app/config.toml，"
    log "      请检查 Dockerfile 的 COPY 指令是否被改动过。"
    exit 1
fi
log "配置文件   : ${Kuantix_CONFIG}"

# -----------------------------------------------------------------------------
# 2. 监听地址与端口
#    HOST 必须是 0.0.0.0：config.toml 默认 127.0.0.1 只接受容器内部访问，
#    平台的健康检查和外部流量都进不来，服务会被判定为启动失败。
# -----------------------------------------------------------------------------
export Kuantix__SERVER__HOST="0.0.0.0"
export Kuantix__SERVER__PORT="${PORT:-8899}"
log "监听地址   : ${Kuantix__SERVER__HOST}:${Kuantix__SERVER__PORT}"

# -----------------------------------------------------------------------------
# 3. 数据根目录与 7 个路径变量
#
#    这里用 ${VAR:-默认值} 写法：如果你在云托管控制台手动配了同名环境变量，
#    控制台的值优先，脚本不会覆盖你。都不配就用 /mnt/Kuantix 下的默认布局。
#
#    /mnt 是 CFS 文件存储在实例内的默认挂载路径。
# -----------------------------------------------------------------------------
Kuantix_DATA_ROOT="${Kuantix_DATA_ROOT:-/mnt/Kuantix}"

export Kuantix__PATHS__ROOT="${Kuantix__PATHS__ROOT:-${Kuantix_DATA_ROOT}}"
export Kuantix__PATHS__DB="${Kuantix__PATHS__DB:-${Kuantix_DATA_ROOT}/db}"
export Kuantix__PATHS__VIPDOC="${Kuantix__PATHS__VIPDOC:-${Kuantix_DATA_ROOT}/vipdoc}"
export Kuantix__PATHS__FACTORS="${Kuantix__PATHS__FACTORS:-${Kuantix_DATA_ROOT}/factors}"
export Kuantix__PATHS__LOGS="${Kuantix__PATHS__LOGS:-${Kuantix_DATA_ROOT}/logs}"
export Kuantix__PATHS__REPORTS="${Kuantix__PATHS__REPORTS:-${Kuantix_DATA_ROOT}/reports}"
export Kuantix__PATHS__EXPORTS="${Kuantix__PATHS__EXPORTS:-${Kuantix_DATA_ROOT}/exports}"

log "数据根目录 : ${Kuantix__PATHS__ROOT}"

# 创建全部数据子目录（幂等，已存在不报错）
mkdir -p \
    "${Kuantix__PATHS__ROOT}" \
    "${Kuantix__PATHS__DB}" \
    "${Kuantix__PATHS__VIPDOC}" \
    "${Kuantix__PATHS__FACTORS}" \
    "${Kuantix__PATHS__LOGS}" \
    "${Kuantix__PATHS__REPORTS}" \
    "${Kuantix__PATHS__EXPORTS}"

# -----------------------------------------------------------------------------
# 4. 数据目录写权限自检
#    CFS 挂载时如果权限选成了「只读」，服务会在第一次写 SQLite 时才崩，
#    报错信息又深又难懂。这里提前探一次，给出人话级别的提示。
# -----------------------------------------------------------------------------
_probe="${Kuantix__PATHS__DB}/.Kuantix_write_probe"
if touch "${_probe}" 2>/dev/null && rm -f "${_probe}" 2>/dev/null; then
    log "写权限自检 : 通过"
else
    log "错误：数据目录 ${Kuantix__PATHS__DB} 不可写。"
    log "      如果你挂载了 CFS 文件存储，请回到云托管服务详情页 ->「存储挂载」，"
    log "      确认该挂载项的权限选的是「读写」而不是「只读」。"
    exit 1
fi

# -----------------------------------------------------------------------------
# 5. 持久化状态提示
#    /mnt 没有被真正挂载时，数据依然能写，但落在容器本地磁盘上，重启即丢。
#    这里做一次探测，把风险明明白白打在日志里，避免用户以为数据是安全的。
# -----------------------------------------------------------------------------
if mountpoint -q /mnt 2>/dev/null || grep -qs ' /mnt ' /proc/mounts; then
    log "持久化     : 检测到 /mnt 已挂载外部存储，数据可跨重启保留"
else
    log "警告：未检测到 /mnt 的外部存储挂载。"
    log "      数据将写入容器本地磁盘，实例重启 / 重新部署后会全部丢失。"
    log "      如需持久化，请在云托管服务详情页 ->「存储挂载」中挂载 CFS 文件存储。"
fi

# -----------------------------------------------------------------------------
# 6. 打印生效的 Kuantix 环境变量，便于排查「变量名拼错导致启动失败」
# -----------------------------------------------------------------------------
log "----------------- 生效的 Kuantix 环境变量 -----------------"
env | grep -E '^Kuantix' | sort | while IFS= read -r _line; do
    log "  ${_line}"
done
log "-----------------------------------------------------------"

# -----------------------------------------------------------------------------
# 7. 启动服务
#    用 exec 替换当前进程：让 uvicorn 成为 PID 1，
#    这样平台发来的停止信号（SIGTERM）能被 Python 直接收到，实现优雅退出。
# -----------------------------------------------------------------------------
log "启动命令   : Kuantix serve"
log "==========================================================="
exec Kuantix serve
