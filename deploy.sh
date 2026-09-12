#!/usr/bin/env bash
# Resale-Watcher 一键部署到 NAS / Linux（Debian/Ubuntu LXC 或裸机，root 运行）。
#
#   bash deploy.sh
#
# 四件事：装 uv（自带独立 Python，不依赖系统 python）→ 建 .venv 装依赖 →
# 备好 .env 并验证能连上 MySQL → 生成 systemd 服务并启动。
#
# 幂等：任何一步失败会立刻停下并报错，修完重跑即可；
#      升级路径 `git pull && bash deploy.sh` 也能反复跑。
# 路径全部由脚本自身位置推导，仓库克隆到哪都行。
set -euo pipefail

SVC=resale-watcher
PYVER=3.12

# ---- 1. 定位项目 ----
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$APP/main.py" ]          || { echo "❌ $APP 下没有 main.py —— deploy.sh 必须放在项目根目录"; exit 1; }
[ -f "$APP/requirements.txt" ] || { echo "❌ $APP 下没有 requirements.txt"; exit 1; }
[ "$(id -u)" -eq 0 ]           || { echo "❌ 需要 root（要写 /etc/systemd/system）"; exit 1; }
echo "✔ 项目目录: $APP"

# ---- 2. uv ----
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "→ 安装 uv …"
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { echo "❌ uv 安装失败，检查能否联网"; exit 1; }
echo "✔ uv $(uv --version)"

# ---- 3. 虚拟环境 ----
cd "$APP"
uv python install "$PYVER"
# 【必须先判存在再建】`uv venv` 撞上已有目录会直接报错退出，set -e 会让整个脚本中止 ——
# 升级路径 `git pull && bash deploy.sh` 会 100% 停在这里，旧代码继续跑而你以为已经升级了。
if [ ! -x "$APP/.venv/bin/python" ] || ! "$APP/.venv/bin/python" -V 2>/dev/null | grep -q "$PYVER"; then
  systemctl stop "$SVC" 2>/dev/null || true      # 别在运行中的解释器脚下换掉 site-packages
  uv venv --clear --python "$PYVER"
fi
uv pip install -r requirements.txt

PY="$APP/.venv/bin/python"
[ -x "$PY" ] || { echo "❌ 虚拟环境没建出来：$PY"; exit 1; }
"$PY" -c "import httpx, nicegui, pymysql, cryptography" || { echo "❌ 依赖没装全"; exit 1; }
echo "✔ 依赖 OK（$("$PY" -V)）"

# ---- 4. 配置 ----
if [ ! -f .env ]; then
  cp .env.example .env
  echo "⚠ 已从 .env.example 生成 .env —— 里面的数据库密码是示例值，改完再往下跑"
fi
# .env 里存着数据库密码，别让同机其它账号读到
chmod 600 .env

# 数据库连不上的话，服务起来也只会在日志里刷错误 —— 不如在这里就说清楚
echo "→ 验证数据库连接 …"
"$PY" - <<'PYEOF' || { echo "❌ 连不上数据库，检查 .env 里的 DB_HOST/DB_USER/DB_PASSWORD 和网络"; exit 1; }
import sys
sys.path.insert(0, ".")
from db import store
store.init_schema()
n = len(store.get_rules())
print(f"✔ 数据库 OK，建表完成，现有 {n} 条规则" + ("（跑 ./run.sh seed 写入起步规则）" if n == 0 else ""))
PYEOF

# WEB_HOST 默认 127.0.0.1（见 config.py），NAS 上不改成 0.0.0.0 就只有本机能访问
grep -q '^WEB_HOST=' .env || echo "WEB_HOST=0.0.0.0" >> .env
sed -i 's/^WEB_HOST=127.0.0.1$/WEB_HOST=0.0.0.0/' .env
echo "✔ $(grep '^WEB_HOST=' .env)"

# ---- 5. systemd ----
cat > "/etc/systemd/system/${SVC}.service" <<EOF
[Unit]
Description=Resale-Watcher
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP
ExecStart=$PY main.py
Restart=always
RestartSec=10
NoNewPrivileges=yes
# 本服务不需要写 $APP 以外的任何地方，也不需要提权
PrivateTmp=yes
# 日志：应用自己写 logs/watch.log（带轮转），systemd 这边只会收到启动期的输出
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SVC" >/dev/null 2>&1 || true
systemctl restart "$SVC"
# 起得慢一点也正常：要建表、建 Mercari 客户端
for _ in $(seq 10); do systemctl is-active --quiet "$SVC" && break; sleep 1; done

if systemctl is-active --quiet "$SVC"; then
  PORT="$(sed -n 's/^WEB_PORT=\([0-9]*\).*/\1/p' .env | head -1)"; PORT="${PORT:-2334}"
  IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  echo
  echo "✅ 部署完成 → http://${IP:-<NAS_IP>}:${PORT}"
  echo "   ⚠ 已绑 0.0.0.0，本面板【无鉴权】—— 局域网内谁都能改你的监控规则。"
  echo "     真要暴露到不可信网络，请在 NAS 的反向代理上加一层 Basic Auth。"
  echo
  echo "   日志: journalctl -u ${SVC} -f   或   tail -f ${APP}/logs/watch.log"
  echo "   状态: ${APP}/run.sh status"
  echo "   升级: git pull && bash deploy.sh"
else
  echo "❌ 启动失败，最近日志："
  journalctl -u "$SVC" -n 30 --no-pager
  exit 1
fi
