# 📺 StreamLive — 接收流并直播视频

> 类似 YouTube 直播的推流接收 + 播放平台。内置 nginx-rtmp 接收推流，自动转 HLS 播放。

## ✨ 功能

- **RTMP 推流接入** — 内置 nginx-rtmp 服务端，别人用 OBS/FFmpeg 推流即可直播
- **推流鉴权** — 每个频道独立密钥，on_publish 回调验证（类似 YouTube 推流密钥）
- **HLS 自动转码** — 推流自动转 HLS 切片，浏览器直接播放
- **拉流模式** — 也可填源 URL 用 FFmpeg 主动拉取（RTMP/RTSP/HLS/SRT）
- **Web 播放器** — 基于 HLS.js，自动重连、错误恢复
- **多频道管理** — 创建多个独立直播频道
- **Docker 部署** — 一行命令启动

## 🚀 快速开始

```bash
cd /root/stream-live
docker compose up -d
```

访问：
- 首页: `http://<IP>:5201`
- 管理: `http://<IP>:5201/login` (admin / admin123)

## 📖 推流方法

### YouTube 式推流（OBS）

1. 管理后台创建频道，获取**推流密钥**
2. OBS → 设置 → 推流
3. 服务器: `rtmp://<IP>:1935/live`
4. 推流密钥: `<频道密钥>`
5. 点击"开始推流"

### FFmpeg 命令行推流

```bash
ffmpeg -re -i video.mp4 -c copy -f flv rtmp://<IP>:1935/live/<推流密钥>
```

### 服务端拉流

在频道设置中填写源 URL，点击"拉流"按钮。

## ⚙️ 配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ADMIN_USER` | admin | 管理员用户名 |
| `ADMIN_PASS` | admin123 | 管理员密码 |
| `SECRET_KEY` | 自动生成 | Session密钥（持久化） |
| `SERVER_HOST` | 空 | 外部IP/域名（展示推流地址用） |

## 🏗️ 架构

```
OBS/FFmpeg 推流
    │
    ▼
nginx-rtmp (:1935)  ←── 接收 RTMP + 推流鉴权(on_publish回调)
    │                    自动转 HLS 切片
    ▼
HLS 文件 (/app/data/hls/<stream_key>/)
    │
    ▼
Flask (:5200→:5201)  ←── HLS 文件服务 + Web UI + REST API
    │
    ▼
浏览器 HLS.js 播放器
```

## 📂 项目结构

```
stream-live/
├── app.py              # Flask 后端
├── templates/          # 页面模板
├── static/css/         # 暗色主题样式
├── nginx-rtmp.conf     # nginx-rtmp 配置
├── supervisord.conf    # 进程管理(nginx+gunicorn)
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## 🔧 技术栈

- Flask 3.0 + Gunicorn + SQLite
- nginx-rtmp（RTMP 接收 + HLS 转码）
- FFmpeg（拉流模式）
- HLS.js 1.5（浏览器播放）
- Docker + supervisord

## 📝 致谢

- [Flask](https://flask.palletsprojects.com/) — Web 框架
- [nginx-rtmp-module](https://github.com/arut/nginx-rtmp-module) — RTMP 服务
- [FFmpeg](https://ffmpeg.org/) — 流处理
- [HLS.js](https://github.com/video-dev/hls.js/) — HLS 播放器
- [Font Awesome](https://fontawesome.com/) — 图标

## License

MIT
