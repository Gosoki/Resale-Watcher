#!/usr/bin/env bash
# Resale-Watcher 管理脚本。所有路径由脚本自身位置推导，仓库放哪都行。
#
#   ./run.sh start|stop|restart|status|logs|fg
#   ./run.sh seed              写入两条起步规则
#   ./run.sh once [规则ID]      立刻跑一轮就退出（不启面板）
#   ./run.sh replay [规则ID]    零请求重放：改完排除词先用它看会不会误杀
#   ./run.sh test              跑匹配逻辑的回归测试
#   ./run.sh initdb            建表（start 时会自动建，一般用不到）
set -euo pipefail

APP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$APP/.venv/bin/python"
LOGDIR="$APP/logs"
PIDFILE="$LOGDIR/watch.pid"
LOGFILE="$LOGDIR/watch.log"

mkdir -p "$LOGDIR"
[ -x "$PY" ] || { echo "❌ 没有虚拟环境：$PY"; echo "   先跑：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }

SVC=resale-watcher

# 【systemd 和 pid 文件两种都要认】NAS 上服务由 systemd 拉起，不写 pid 文件。
# 只看 pid 文件的话 status 永远报「未运行」，而 start 还会再起一个实例，
# 两个进程同时对外发请求 —— 而 deploy.sh 结尾正是让你用 run.sh status。
systemd_running() {
  command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$SVC" 2>/dev/null
}

# pid 文件里的进程不仅要活着，还得真是本项目的 —— 重启后 pid 可能被别的进程复用，
# 那样 start 会假称「已经在跑了」，stop 会去杀无关进程。
pidfile_running() {
  [ -f "$PIDFILE" ] || return 1
  local pid; pid="$(cat "$PIDFILE" 2>/dev/null)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o command= 2>/dev/null | grep -q "main.py"
}

running() { systemd_running || pidfile_running; }

port() { sed -n 's/^WEB_PORT=\([0-9]*\).*/\1/p' "$APP/.env" 2>/dev/null | head -1; }

case "${1:-}" in
  start)
    systemd_running && {
      echo "systemd 服务 $SVC 正在运行 —— 用 systemctl restart $SVC，别用这个脚本"
      echo "（再起一个实例会让两个进程同时对外发请求）"; exit 0; }
    pidfile_running && { echo "已经在跑了（PID $(cat "$PIDFILE")）"; exit 0; }
    cd "$APP"
    # 【不能重定向到 $LOGFILE】那是 RotatingFileHandler 正在写并轮转的文件：
    # 轮转把它改名之后，这里的 fd 还指着旧 inode，stderr 全写进被改名的文件，
    # 最终随轮转被删。而未捕获异常的堆栈恰恰只走 stderr。
    nohup "$PY" main.py >>"$LOGDIR/stdout.log" 2>&1 &
    echo $! > "$PIDFILE"
    # 【等端口真的能连上，不是干睡几秒】NiceGUI 起来要几秒（建表、连库、绑端口），
    # 固定 sleep 要么白等要么不够 —— 不够的时候脚本报「已启动」而浏览器打开是连接被拒。
    P="$(port)"; P="${P:-2334}"
    for _ in $(seq 40); do
      running || break
      "$PY" -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',$P))==0 else 1)" 2>/dev/null && break
      sleep 0.5
    done
    if running; then
      echo "✅ 已启动（PID $(cat "$PIDFILE")）→ http://127.0.0.1:$(port)"
      echo "   日志：./run.sh logs"
    else
      echo "❌ 启动失败，最后 30 行："; tail -30 "$LOGDIR/stdout.log" "$LOGFILE" 2>/dev/null
      rm -f "$PIDFILE"; exit 1
    fi
    ;;
  stop)
    systemd_running && { echo "由 systemd 管着，用 systemctl stop $SVC"; exit 1; }
    pidfile_running || { echo "没在跑"; rm -f "$PIDFILE"; exit 0; }
    PID="$(cat "$PIDFILE")"
    kill "$PID"
    # 等它自己收尾：轮询线程可能正卡在一次请求的 3〜8 秒间隔里
    for _ in $(seq 20); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
    kill -9 "$PID" 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "✅ 已停止"
    ;;
  restart) "$0" stop; "$0" start ;;
  fg)      cd "$APP"; exec "$PY" main.py ;;
  logs)    tail -f "$LOGFILE" ;;
  status)
    if systemd_running; then
      echo "● 运行中（systemd: $SVC）→ http://127.0.0.1:$(port)"
    elif pidfile_running; then
      echo "● 运行中（PID $(cat "$PIDFILE")）→ http://127.0.0.1:$(port)"
    else
      echo "○ 未运行"
    fi
    cd "$APP" && "$PY" - <<'PYEOF'
from db import store
import config
try:
    d = store.today_stat()
    print(f"  今日请求 {d['requests']}/{store.get_settings()['daily_request_limit']}，失败 {d['errors']}")
    import sources
    for r in store.get_rules():
        st = store.get_state(r["id"])
        s = store.one("SELECT COUNT(*) t, SUM(matched) h FROM item WHERE rule_id=%s", (r["id"],))
        live = store.one("SELECT COUNT(*) n FROM item WHERE rule_id=%s AND matched=1 "
                         "AND status='on_sale'", (r["id"],))["n"]
        med = f"¥{st['median_price']:,}" if st["median_price"] else "样本不足"
        flag = "" if r["enabled"] else " [停用]"
        print(f"  #{r['id']} {r['name']}{flag}  入库{s['t'] or 0}  "
              f"在售命中{live}（累计{int(s['h'] or 0)}）  市价中位 {med}（跨源{st['sample_count']}件）")
        for src in sources.for_rule(r):
            ss = store.get_source_state(r["id"], src.key)
            n = store.one("SELECT COUNT(*) t, "
                          "SUM(matched = 1 AND status = 'on_sale') h "
                          "FROM item WHERE rule_id=%s AND source=%s", (r["id"], src.key))
            last = f"{ss['last_scan_at']:%m-%d %H:%M}" if ss["last_scan_at"] else "还没扫过"
            print(f"       {src.name:<14} 平台在售{ss['last_total']:>4}  入库{n['t'] or 0:>3}  在售命中{int(n['h'] or 0):<3}  上次 {last}")
            if ss["last_error"]:
                print(f"         ⚠ {ss['last_error'][:70]}")
except Exception as e:
    print("  读数据库失败：", e)
PYEOF
    ;;
  seed)    cd "$APP"; "$PY" tools/seed.py ;;
  replay)  cd "$APP"; shift; "$PY" tools/replay.py "$@" ;;
  test)    cd "$APP"; "$PY" -m pytest tests/ -q ;;
  initdb)  cd "$APP"; "$PY" -c "from db import store; store.init_schema(); print('✅ 建表完成')" ;;
  once)
    cd "$APP"; shift
    "$PY" - "$@" <<'PYEOF'
import logging, sys
sys.path.insert(0, ".")
from main import setup_logging
setup_logging(console=True)
from core import poller
from db import store
rules = [store.get_rule(int(sys.argv[1]))] if len(sys.argv) > 1 else store.get_rules(enabled_only=True)
for r in filter(None, rules):
    poller.run_once(r)
PYEOF
    ;;
  *)
    sed -n '2,9p' "$0" | sed 's/^# \?//'
    exit 1
    ;;
esac
