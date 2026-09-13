#!/usr/bin/env python3
"""
StreamLive — 接收流并直播视频的 Web 应用

功能：
  - 接收 RTMP 推流（nginx-rtmp），类似 YouTube 直播
  - 也可主动拉流（FFmpeg 从源 URL 拉取 → HLS）
  - 推流鉴权（on_publish 回调验证 stream key）
  - Web 界面展示频道列表 + HLS.js 播放器

技术栈：Flask + nginx-rtmp + FFmpeg + HLS.js + Docker

推流地址: rtmp://<IP>:1935/live/<stream_key>
播放地址: http://<IP>:5201/live/<stream_key>/index.m3u8
"""

import os
import re
import uuid
import json
import signal
import sqlite3
import secrets
import logging
import subprocess
import threading
import time
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify, session,
    redirect, url_for, send_from_directory, abort, g, Response
)

# ─── 配置 ─────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
HLS_DIR = DATA_DIR / "hls"
COVERS_DIR = DATA_DIR / "covers"          # 频道封面目录
SNAPSHOTS_DIR = DATA_DIR / "snapshots"    # 版本快照目录
DB_PATH = DATA_DIR / "streamlive.db"
SECRET_KEY = os.environ.get("SECRET_KEY", "")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")

# 获取外部访问地址（用于展示推流 URL）
SERVER_HOST = os.environ.get("SERVER_HOST", "")

# 确保证书持久化
if not SECRET_KEY:
    SECRET_FILE = DATA_DIR / "secret_key.txt"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_FILE.exists():
        SECRET_KEY = SECRET_FILE.read_text().strip()
    else:
        SECRET_KEY = secrets.token_hex(32)
        SECRET_FILE.write_text(SECRET_KEY)

HLS_DIR.mkdir(parents=True, exist_ok=True)
COVERS_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

os.environ["TZ"] = "Asia/Shanghai"
import time as _time
_time.tzset()

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.permanent_session_lifetime = 86400  # 24h

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger("streamlive")

# ─── ISO 时间日志正则 ─────────────────────────────
RE_ISO_DATE = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[.\d]*Z?')

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")

# ─── 数据库 ───────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS channels (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        stream_key TEXT NOT NULL UNIQUE,
        source_url TEXT DEFAULT '',
        source_type TEXT DEFAULT 'push',
        status TEXT DEFAULT 'offline',
        created_at TEXT DEFAULT (datetime('now', '+8 hours')),
        updated_at TEXT DEFAULT (datetime('now', '+8 hours')),
        viewers INTEGER DEFAULT 0,
        cover_url TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    conn.commit()
    # Migration: 加 cover_url 字段（旧数据库兼容）
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(channels)").fetchall()]
        if "cover_url" not in cols:
            conn.execute("ALTER TABLE channels ADD COLUMN cover_url TEXT DEFAULT ''")
            conn.commit()
            log.info("数据库迁移: 已添加 cover_url 字段")
    except Exception as e:
        log.warning(f"数据库迁移检查失败: {e}")
    conn.close()


# ─── FFmpeg 拉流管理 ──────────────────────────────
class StreamManager:
    """管理 FFmpeg 拉流进程（pull 模式）"""

    def __init__(self):
        self.processes = {}  # channel_id -> subprocess.Popen
        self.locks = {}
        self._lock = threading.Lock()

    def _get_lock(self, channel_id):
        with self._lock:
            if channel_id not in self.locks:
                self.locks[channel_id] = threading.Lock()
            return self.locks[channel_id]

    def start_pull(self, channel):
        channel_id = channel["id"]
        lock = self._get_lock(channel_id)

        with lock:
            if channel_id in self.processes:
                proc = self.processes[channel_id]
                if proc.poll() is None:
                    log.info(f"频道 {channel_id} 拉流已在运行")
                    return True

            source_url = channel["source_url"]
            stream_key = channel["stream_key"]
            if not source_url:
                log.error(f"频道 {channel_id} 无源 URL")
                return False

            # nginx-rtmp 的 hls_nested 模式按 stream_key 分目录
            stream_dir = HLS_DIR / stream_key
            stream_dir.mkdir(parents=True, exist_ok=True)

            for f in stream_dir.glob("*.ts"):
                f.unlink()
            for f in stream_dir.glob("*.m3u8"):
                f.unlink()

            cmd = [
                FFMPEG_BIN,
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-i", source_url,
                "-c:v", "copy",
                "-c:a", "aac",
                "-ar", "44100",
                "-f", "hls",
                "-hls_time", "2",
                "-hls_list_size", "6",
                "-hls_flags", "delete_segments+append_list",
                "-hls_segment_filename", str(stream_dir / "seg_%05d.ts"),
                str(stream_dir / "index.m3u8"),
            ]

            log.info(f"启动拉流: {channel_id} <- {source_url}")

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )

            self.processes[channel_id] = proc

            threading.Thread(
                target=self._monitor, args=(channel_id, proc),
                daemon=True
            ).start()

            return True

    def _monitor(self, channel_id, proc):
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass

        retcode = proc.wait()
        log.info(f"FFmpeg 进程结束: {channel_id} (exit={retcode})")

        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE channels SET status='offline', updated_at=datetime('now', '+8 hours') WHERE id=?",
                (channel_id,)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"更新频道状态失败: {e}")

        with self._lock:
            self.processes.pop(channel_id, None)

    def stop_pull(self, channel_id):
        lock = self._get_lock(channel_id)

        with lock:
            proc = self.processes.get(channel_id)
            if proc and proc.poll() is None:
                log.info(f"停止拉流: {channel_id}")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)

            self.processes.pop(channel_id, None)
            self._clean_hls(channel_id)

    def _clean_hls(self, channel_id):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT stream_key FROM channels WHERE id=?", (channel_id,)).fetchone()
            conn.close()
            if row:
                stream_dir = HLS_DIR / row["stream_key"]
                if stream_dir.exists():
                    for f in stream_dir.glob("*.ts"):
                        f.unlink()
                    for f in stream_dir.glob("*.m3u8"):
                        f.unlink()
        except Exception:
            pass

    def is_running(self, channel_id):
        proc = self.processes.get(channel_id)
        return proc is not None and proc.poll() is None

    def stop_all(self):
        ids = list(self.processes.keys())
        for cid in ids:
            self.stop_pull(cid)


stream_mgr = StreamManager()


# ─── 观众计数器 ──────────────────────────────────
class ViewerTracker:
    """追踪在线观众人数（内存 + 心跳超时）"""

    def __init__(self, timeout=60):
        self.viewers = {}       # channel_id -> {viewer_id: last_heartbeat}
        self._lock = threading.Lock()
        self.timeout = timeout

    def join(self, channel_id, viewer_id):
        with self._lock:
            if channel_id not in self.viewers:
                self.viewers[channel_id] = {}
            self.viewers[channel_id][viewer_id] = time.time()

    def heartbeat(self, channel_id, viewer_id):
        with self._lock:
            if channel_id in self.viewers:
                self.viewers[channel_id][viewer_id] = time.time()

    def leave(self, channel_id, viewer_id):
        with self._lock:
            if channel_id in self.viewers:
                self.viewers[channel_id].pop(viewer_id, None)

    def count(self, channel_id):
        with self._lock:
            return len(self.viewers.get(channel_id, {}))

    def cleanup(self):
        """清理超时观众（由后台线程定期调用）"""
        now = time.time()
        changed = set()
        with self._lock:
            for cid in list(self.viewers.keys()):
                before = len(self.viewers[cid])
                self.viewers[cid] = {
                    vid: t for vid, t in self.viewers[cid].items()
                    if now - t < self.timeout
                }
                if len(self.viewers[cid]) != before:
                    changed.add(cid)
        # 不同步数据库，只在 API 返回内存计数

    def _sync_db(self, channel_id):
        """同步观众数到数据库（非阻塞，超时1秒）"""
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=1)
            conn.execute(
                "UPDATE channels SET viewers=? WHERE id=?",
                (self.count(channel_id), channel_id)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _cleanup_loop(self):
        while True:
            time.sleep(15)
            self.cleanup()

    def start_cleanup_thread(self):
        threading.Thread(target=self._cleanup_loop, daemon=True).start()


viewer_tracker = ViewerTracker()
viewer_tracker.start_cleanup_thread()


# ─── 版本快照管理 ─────────────────────────────────
JST = timezone(timedelta(hours=8))

class SnapshotManager:
    """以时间为粒度的频道版本快照管理"""

    def __init__(self):
        self._lock = threading.Lock()

    def create_snapshot(self, channel_id, action, data=None):
        """创建快照
        action: 'create' | 'update' | 'delete' | 'cover' | 'start' | 'stop'
        data: 变更前的频道数据 dict
        """
        ts = datetime.now(JST).strftime('%Y%m%d-%H%M%S')
        snap_id = f"{ts}-{action}"

        snap_dir = SNAPSHOTS_DIR / channel_id
        snap_dir.mkdir(parents=True, exist_ok=True)

        snap_file = snap_dir / f"{snap_id}.json"
        snap_data = {
            "id": snap_id,
            "channel_id": channel_id,
            "action": action,
            "timestamp": datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S'),
            "data": data or {}
        }

        with open(snap_file, 'w', encoding='utf-8') as f:
            json.dump(snap_data, f, ensure_ascii=False, indent=2)

        # 如果有封面，也备份一份
        if data and data.get("cover_url"):
            cover_name = Path(data["cover_url"]).name
            cover_src = COVERS_DIR / cover_name
            if cover_src.exists():
                cover_dst = snap_dir / f"{snap_id}-cover{Path(cover_name).suffix}"
                shutil.copy2(str(cover_src), str(cover_dst))

        # 清理超过 100 份的旧快照
        self._cleanup(channel_id)
        return snap_id

    def list_snapshots(self, channel_id):
        """列出频道所有快照"""
        snap_dir = SNAPSHOTS_DIR / channel_id
        if not snap_dir.exists():
            return []
        snaps = []
        for f in sorted(snap_dir.glob("*.json"), reverse=True):
            try:
                with open(f, 'r', encoding='utf-8') as fh:
                    snaps.append(json.load(fh))
            except Exception:
                continue
        return snaps

    def get_snapshot(self, channel_id, snap_id):
        """获取指定快照"""
        snap_file = SNAPSHOTS_DIR / channel_id / f"{snap_id}.json"
        if not snap_file.exists():
            return None
        with open(snap_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def restore_snapshot(self, channel_id, snap_id):
        """恢复到指定快照"""
        snap = self.get_snapshot(channel_id, snap_id)
        if not snap:
            return False
        data = snap.get("data", {})
        if not data:
            return False

        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE channels SET name=?, description=?, source_url=?, cover_url=?, updated_at=datetime('now', '+8 hours') WHERE id=?",
            (data.get("name",""), data.get("description",""), data.get("source_url",""),
             data.get("cover_url",""), channel_id)
        )
        conn.commit()
        conn.close()

        # 恢复封面文件
        snap_dir = SNAPSHOTS_DIR / channel_id
        cover_snap = next(snap_dir.glob(f"{snap_id}-cover.*"), None)
        if cover_snap and data.get("cover_url"):
            cover_name = Path(data["cover_url"]).name
            shutil.copy2(str(cover_snap), str(COVERS_DIR / cover_name))

        return True

    def _cleanup(self, channel_id):
        """每个频道保留最近 100 份快照"""
        snap_dir = SNAPSHOTS_DIR / channel_id
        files = sorted(snap_dir.glob("*.json"), reverse=True)
        for f in files[100:]:
            f.unlink()
            # 也删关联的封面备份
            for c in snap_dir.glob(f"{f.stem}-cover.*"):
                c.unlink()

    def list_all_snapshots(self):
        """列出所有频道的快照（按时间倒序）"""
        all_snaps = []
        if not SNAPSHOTS_DIR.exists():
            return []
        for ch_dir in SNAPSHOTS_DIR.iterdir():
            if not ch_dir.is_dir():
                continue
            for f in ch_dir.glob("*.json"):
                try:
                    with open(f, 'r', encoding='utf-8') as fh:
                        all_snaps.append(json.load(fh))
                except Exception:
                    continue
        all_snaps.sort(key=lambda x: x.get("timestamp",""), reverse=True)
        return all_snaps[:50]


snapshot_mgr = SnapshotManager()


# ─── 鉴权 ─────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def api_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return jsonify({"error": "未登录"}), 401
        return f(*args, **kwargs)
    return decorated


def gen_stream_key():
    return secrets.token_hex(8)


# ─── 页面路由 ─────────────────────────────────────
@app.route("/")
def index():
    db = get_db()
    channels = db.execute(
        "SELECT * FROM channels ORDER BY created_at DESC"
    ).fetchall()
    return render_template("index.html", channels=channels)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if username == ADMIN_USER and password == ADMIN_PASS:
            session.permanent = True
            session["logged_in"] = True
            return redirect(url_for("admin"))

        return render_template("login.html", error="用户名或密码错误")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/admin")
@login_required
def admin():
    db = get_db()
    channels = db.execute(
        "SELECT * FROM channels ORDER BY created_at DESC"
    ).fetchall()
    return render_template("admin.html", channels=channels)


@app.route("/watch/<channel_id>")
def watch(channel_id):
    db = get_db()
    channel = db.execute(
        "SELECT * FROM channels WHERE id=?", (channel_id,)
    ).fetchone()
    if not channel:
        abort(404)
    return render_template("watch.html", channel=channel)


# ─── HLS 播放路由 ─────────────────────────────────
# nginx-rtmp hls_nested 模式: /app/data/hls/<stream_key>/index.m3u8
@app.route("/live/<stream_key>/index.m3u8")
def hls_playlist(stream_key):
    m3u8 = HLS_DIR / stream_key / "index.m3u8"
    if not m3u8.exists():
        abort(404)
    resp = send_from_directory(str(HLS_DIR / stream_key), "index.m3u8",
                                mimetype="application/vnd.apple.mpegurl")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/live/<stream_key>/<path:filename>.ts")
def hls_segment(stream_key, filename):
    # 匹配 nginx-rtmp hls_nested 模式生成的 TS 文件（数字序列如 0.ts, 1.ts）
    ts_file = HLS_DIR / stream_key / f"{filename}.ts"
    if not ts_file.exists():
        abort(404)
    resp = send_from_directory(str(HLS_DIR / stream_key), f"{filename}.ts",
                                mimetype="video/mp2t")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ─── RTMP 推流鉴权 (nginx-rtmp on_publish 回调) ──
@app.route("/api/rtmp/auth", methods=["POST"])
def rtmp_auth():
    """nginx-rtmp on_publish 回调

    nginx 发送 POST 请求，body 包含:
      app=live&flashver=&swfurl=&tcurl=rtmp://IP:1935/live&pageurl=&addr=...&clientid=...&name=<stream_key>

    返回 200 允许推流，返回 403 拒绝。
    """
    stream_key = request.form.get("name", "")
    if not stream_key:
        log.warning("RTMP 推流被拒: 无 stream key")
        return "Forbidden", 403

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    channel = conn.execute(
        "SELECT id, name FROM channels WHERE stream_key=?", (stream_key,)
    ).fetchone()

    if not channel:
        log.warning(f"RTMP 推流被拒: 未知 stream_key={stream_key}")
        conn.close()
        return "Forbidden", 403

    # 更新频道状态为 live
    conn.execute(
        "UPDATE channels SET status='live', updated_at=datetime('now', '+8 hours') WHERE id=?",
        (channel["id"],)
    )
    conn.commit()
    conn.close()

    log.info(f"RTMP 推流已授权: channel={channel['name']} key={stream_key}")
    return "OK", 200


@app.route("/api/rtmp/done", methods=["POST"])
def rtmp_done():
    """nginx-rtmp on_publish_done 回调 — 推流结束"""
    stream_key = request.form.get("name", "")
    if not stream_key:
        return "OK", 200

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "UPDATE channels SET status='offline', updated_at=datetime('now', '+8 hours') WHERE stream_key=?",
        (stream_key,)
    )
    conn.commit()
    conn.close()

    log.info(f"RTMP 推流结束: key={stream_key}")
    return "OK", 200


# ─── REST API ────────────────────────────────────
@app.route("/api/channels")
def api_channels():
    db = get_db()
    channels = db.execute(
        "SELECT id, name, description, status, viewers, created_at, cover_url FROM channels ORDER BY created_at DESC"
    ).fetchall()
    return jsonify([dict(c) for c in channels])


@app.route("/api/channels", methods=["POST"])
@api_login_required
def api_create_channel():
    data = request.get_json()
    if not data or not data.get("name"):
        return jsonify({"error": "频道名称不能为空"}), 400

    channel_id = uuid.uuid4().hex[:12]
    stream_key = gen_stream_key()

    db = get_db()
    db.execute(
        """INSERT INTO channels (id, name, description, stream_key, source_url, source_type)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            channel_id,
            data["name"],
            data.get("description", ""),
            stream_key,
            data.get("source_url", ""),
            data.get("source_type", "push"),
        )
    )
    db.commit()

    snapshot_mgr.create_snapshot(channel_id, "create", {
        "name": data["name"], "description": data.get("description", ""),
        "source_url": data.get("source_url", ""), "cover_url": ""
    })
    log.info(f"创建频道: {channel_id} ({data['name']})")
    return jsonify({"id": channel_id, "stream_key": stream_key})


@app.route("/api/channels/<channel_id>/cover", methods=["POST"])
@api_login_required
def api_upload_cover(channel_id):
    """上传频道封面"""
    if 'cover' not in request.files:
        return jsonify({"error": "未选择文件"}), 400

    file = request.files['cover']
    if not file.filename:
        return jsonify({"error": "未选择文件"}), 400

    # 只允许图片
    allowed = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed:
        return jsonify({"error": f"不支持的格式，仅支持 {', '.join(allowed)}"}), 400

    # 生成文件名
    cover_name = f"{channel_id}{ext}"
    file.save(str(COVERS_DIR / cover_name))

    # 更新数据库
    db = get_db()
    db.execute("UPDATE channels SET cover_url=?, updated_at=datetime('now', '+8 hours') WHERE id=?",
               (f"/covers/{cover_name}", channel_id))
    db.commit()

    snapshot_mgr.create_snapshot(channel_id, "cover", {
        "name": "", "description": "", "source_url": "",
        "cover_url": f"/covers/{cover_name}"
    })
    log.info(f"频道 {channel_id} 封面上传: {cover_name}")
    return jsonify({"ok": True, "cover_url": f"/covers/{cover_name}"})


@app.route("/api/channels/<channel_id>", methods=["DELETE"])
@api_login_required
def api_delete_channel(channel_id):
    stream_mgr.stop_pull(channel_id)
    db = get_db()
    channel = db.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    old_data = dict(channel) if channel else {}
    db.execute("DELETE FROM channels WHERE id=?", (channel_id,))
    db.commit()

    import shutil
    stream_dir = HLS_DIR / channel_id
    if stream_dir.exists():
        shutil.rmtree(stream_dir, ignore_errors=True)

    snapshot_mgr.create_snapshot(channel_id, "delete", old_data)
    log.info(f"删除频道: {channel_id}")
    return jsonify({"ok": True})


@app.route("/api/channels/<channel_id>", methods=["PUT"])
@api_login_required
def api_update_channel(channel_id):
    data = request.get_json()
    db = get_db()
    channel = db.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if not channel:
        return jsonify({"error": "频道不存在"}), 404

    name = data.get("name", channel["name"])
    description = data.get("description", channel["description"])
    source_url = data.get("source_url", channel["source_url"])

    db.execute(
        "UPDATE channels SET name=?, description=?, source_url=?, updated_at=datetime('now', '+8 hours') WHERE id=?",
        (name, description, source_url, channel_id)
    )

    # 处理封面删除
    if data.get("remove_cover"):
        db.execute("UPDATE channels SET cover_url='' WHERE id=?", (channel_id,))
        # 删除文件
        for f in COVERS_DIR.glob(f"{channel_id}.*"):
            f.unlink()
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/channels/<channel_id>/start", methods=["POST"])
@api_login_required
def api_start_stream(channel_id):
    db = get_db()
    channel = db.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if not channel:
        return jsonify({"error": "频道不存在"}), 404

    if not channel["source_url"] or not channel["source_url"].strip():
        return jsonify({"error": "请先设置源 URL"}), 400

    ok = stream_mgr.start_pull(dict(channel))
    if ok:
        db.execute(
            "UPDATE channels SET status='live', updated_at=datetime('now', '+8 hours') WHERE id=?",
            (channel_id,)
        )
        db.commit()
        return jsonify({"ok": True, "message": "拉流已启动"})
    return jsonify({"error": "启动失败"}), 500


@app.route("/api/channels/<channel_id>/stop", methods=["POST"])
@api_login_required
def api_stop_stream(channel_id):
    stream_mgr.stop_pull(channel_id)
    db = get_db()
    db.execute(
        "UPDATE channels SET status='offline', updated_at=datetime('now', '+8 hours') WHERE id=?",
        (channel_id,)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/channels/<channel_id>/status")
def api_channel_status(channel_id):
    db = get_db()
    channel = db.execute(
        "SELECT id, name, status, viewers FROM channels WHERE id=?", (channel_id,)
    ).fetchone()
    if not channel:
        return jsonify({"error": "频道不存在"}), 404

    running = stream_mgr.is_running(channel_id)
    # 也检查 HLS 文件是否存在（RTMP 推流时 FFmpeg 不运行但 HLS 有文件）
    if not running:
        ch = db.execute("SELECT stream_key FROM channels WHERE id=?", (channel_id,)).fetchone()
        if ch:
            m3u8 = HLS_DIR / ch["stream_key"] / "index.m3u8"
            if m3u8.exists():
                running = True

    return jsonify({
        "id": channel["id"],
        "name": channel["name"],
        "status": "live" if running else channel["status"],
        "ffmpeg_running": stream_mgr.is_running(channel_id),
        "viewers": viewer_tracker.count(channel_id)
    })


@app.route("/api/channels/<channel_id>/join", methods=["POST"])
def api_join(channel_id):
    """观众加入"""
    import uuid as _uuid
    viewer_id = request.json.get("viewer_id") if request.is_json else None
    if not viewer_id:
        viewer_id = _uuid.uuid4().hex[:12]
    viewer_tracker.join(channel_id, viewer_id)
    return jsonify({"viewer_id": viewer_id, "viewers": viewer_tracker.count(channel_id)})


@app.route("/api/channels/<channel_id>/heartbeat", methods=["POST"])
def api_heartbeat(channel_id):
    """观众心跳"""
    viewer_id = request.json.get("viewer_id", "") if request.is_json else ""
    if viewer_id:
        viewer_tracker.heartbeat(channel_id, viewer_id)
    return jsonify({"viewers": viewer_tracker.count(channel_id)})


@app.route("/api/channels/<channel_id>/leave", methods=["POST"])
def api_leave(channel_id):
    """观众离开"""
    viewer_id = request.json.get("viewer_id", "") if request.is_json else ""
    if viewer_id:
        viewer_tracker.leave(channel_id, viewer_id)
    return jsonify({"viewers": viewer_tracker.count(channel_id)})


@app.route("/api/status")
def api_status():
    db = get_db()
    total = db.execute("SELECT COUNT(*) as c FROM channels").fetchone()["c"]
    live = db.execute("SELECT COUNT(*) as c FROM channels WHERE status='live'").fetchone()["c"]
    return jsonify({
        "channels_total": total,
        "channels_live": live,
        "ffmpeg_processes": len([p for p in stream_mgr.processes.values() if p.poll() is None])
    })


@app.route("/test")
def test_page():
    return render_template("test.html")


# ─── 版本管理 API ───────────────────────────────
@app.route("/api/snapshots")
@api_login_required
def api_list_snapshots():
    """列出所有频道快照"""
    channel_id = request.args.get("channel_id")
    if channel_id:
        snaps = snapshot_mgr.list_snapshots(channel_id)
    else:
        snaps = snapshot_mgr.list_all_snapshots()
    return jsonify(snaps)


@app.route("/api/snapshots/<channel_id>/<snap_id>")
@api_login_required
def api_get_snapshot(channel_id, snap_id):
    """获取指定快照详情"""
    snap = snapshot_mgr.get_snapshot(channel_id, snap_id)
    if not snap:
        return jsonify({"error": "快照不存在"}), 404
    return jsonify(snap)


@app.route("/api/snapshots/<channel_id>/<snap_id>/restore", methods=["POST"])
@api_login_required
def api_restore_snapshot(channel_id, snap_id):
    """恢复到指定快照"""
    ok = snapshot_mgr.restore_snapshot(channel_id, snap_id)
    if ok:
        log.info(f"恢复频道 {channel_id} 到快照 {snap_id}")
        return jsonify({"ok": True})
    return jsonify({"error": "恢复失败"}), 400


# ─── 封面图片服务 ───────────────────────────────
@app.route("/covers/<path:filename>")
def serve_cover(filename):
    return send_from_directory(str(COVERS_DIR), filename)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ─── 优雅退出 ─────────────────────────────────────
def graceful_shutdown(signum, frame):
    log.info("收到退出信号，停止所有 FFmpeg 进程...")
    stream_mgr.stop_all()
    log.info("所有进程已停止")
    os._exit(0)


signal.signal(signal.SIGTERM, graceful_shutdown)
signal.signal(signal.SIGINT, graceful_shutdown)


# ─── 启动 ─────────────────────────────────────────
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5200))
    log.info(f"StreamLive 启动于 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
