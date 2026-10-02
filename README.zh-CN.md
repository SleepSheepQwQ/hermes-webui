# 移动端实践中改良的 Hermes Web UI

> 本仓库是 [nesquena/hermes-webui](https://github.com/nesquena/hermes-webui) 的 **Fork**，
> 记录在移动端（Android / Termux）实际使用中遇到的问题与改良。
> 上游项目：<https://github.com/nesquena/hermes-webui> · MIT License

本文档只描述**相对上游的差异**。上游的完整功能说明、安装步骤、截图，
请直接看 [上游 README](https://github.com/nesquena/hermes-webui#readme)。

---

## 为什么有这个 Fork

上游 Hermes Web UI 主要面向服务器 / 桌面场景。在 Android + Termux 上直接使用时，
会因为移动端特有的环境限制出现卡顿与体验问题。本 Fork 记录这些问题的定位过程
和可行修法，并在合适的时候向上游提交 PR。

## 移动端环境

| 项目 | 值 |
| --- | --- |
| 设备 | Android 14 (SDK 34) |
| 运行环境 | Termux（无 root 依赖） |
| 架构 | aarch64 / arm64 |
| Python | 3.13 / 3.14 |
| 访问方式 | 浏览器（本机 127.0.0.1 或局域网） |

## 已定位的问题

### 1. 界面卡顿、输入抽搐

**现象**：输入框打字时界面抽搐，会话切换明显迟滞。

**定位**：从 `~/.hermes/webui.log` 的 `[SLOW]` 记录看到，所有耗时都堆在
`acquired_lock` 阶段：

```
[SLOW] /api/session/draft total=75961.4ms
       stages: after_get_session=48.8ms acquired_lock=75891.8ms
               before_save=0.0ms after_save=12.5ms
```

取会话 48ms、写盘 12ms，**75.9 秒全在等锁**。同一秒内其他会话 API 也全部排队：

```
/api/session?resolve_model=1   117187 ms
/api/session/update            104376 ms
/api/chat/cancel               100291 ms
```

而同秒的静态文件请求只要 0.7–63 ms。说明瓶颈不是 CPU、不是网络，是锁竞争。

**根因**：`Session.save()` 无条件全量序列化整个会话对象：

```python
# api/models.py:1657
payload = json.dumps({**meta, **extra}, ensure_ascii=False, indent=2)
```

而 `composer_draft`（输入框草稿）只是这个对象的一个字段
（见 `api/models.py` 的 `METADATA_FIELDS`）。前端每敲一个字符就触发一次
`/api/session/draft`，每次都走完整 `save()`，把整个会话 JSON 全量重写一遍，
并且和同一会话的 agent 回合保存抢同一把 per-session 锁。

会话文件越大，每次序列化越慢，锁被占越久，排队越严重。

**实测会话文件大小**：

| 会话 | 文件大小 |
| --- | --- |
| 会话 A | 5.85 MB |
| 会话 B | 4.98 MB |
| 会话 C | 1.77 MB |

5.85 MB 的构成：

```
messages           1997 KB  (34%)
context_messages   1945 KB  (33%)   ← 与 messages 高度重复
anchor_activity_scenes 839 KB (14%)
tool_calls          546 KB   (9%)
```

**性质说明**：这是**编程模式问题**，不是 Python 的性能问题。
换任何语言，只要保留"每次击键全量重写大会话 JSON + 粗粒度锁"这个模式，一样会卡。
Python 的 GIL 会让大 JSON 序列化串行执行，起的是放大器作用，不是根因。

### 2. 模型无输出但不报错（静默卡死）

**现象**：模型请求打不通时界面不报错，只是长时间无响应。

**定位**：日志里对应的是 WARNING 级别记录，UI 不展示，所以看起来像静默卡死：

```
WARNING Interrupted provider wait counted as stale after 59s with no output
WARNING OpenAI client aborted (stream_interrupt_abort)
```

上游默认 stale 阈值为 180 秒（上下文超 50k token 自动升到 240 秒，超 100k 升到 300 秒）。

### 3. 上游 400：budget_tokens 与 max_tokens 冲突

```
上游服务错误 (400): {"error":{"message":"invalid Claude request:
thinking: budget_tokens must be less than max_tokens"}}
```

同时日志里出现：

```
ERROR Output-cap error not routed into compression (max_tokens over provider cap)
```

即 Hermes 已识别这是输出上限问题，但没有把它路由进压缩逻辑。

## 移动端调优配置

以下是本机实际使用中的调优项，作为参考记录（**不含任何密钥**）。

上游支持通过环境变量控制 WebUI 行为：

| 变量 | 上游默认 | 移动端取值 | 作用 |
| --- | --- | --- | --- |
| `HERMES_WEBUI_MODELS_REBUILD_BUDGET` | 4 | 2 | 模型目录重建的时间预算（秒），超出则返回磁盘缓存 |
| `HERMES_WEBUI_MAX_SESSION_RESOLVE` | 2 | 4 | 完整会话解析的并发上限 |
| `HERMES_WEBUI_BUDGET_WARN_COOLDOWN` | — | 900 | 超预算告警冷却秒数 |

写进 `hermes-webui/.env`（该文件已在 `.gitignore` 中，不会被提交）。

## 拟议的改良

延续上面「问题 1」的根因，可做的改动：

**草稿独立存储**（尚未实现）

把 `composer_draft` 从会话主 JSON 中拆出，单独存到 `sessions/<sid>.draft.json`：

- 草稿保存只写几 KB 的小文件，不触碰 MB 级主文件
- 主文件仅在会话真正变更（追加消息等）时才全量写入
- 高频小写与低频大写分离，消除锁竞争

这样击键保存的代价从"全量序列化 5.85 MB + 抢锁"降到"写几 KB 小文件"。

## 与上游的关系

- 上游仓库：<https://github.com/nesquena/hermes-webui>
- 上游 License：MIT（本仓库保留）
- 本 Fork 的用途：记录移动端实践中的定位结果与改良
- 计划：验证有效后向上游提交 PR

同步上游：

```bash
git fetch upstream
git merge upstream/master
```

## License

MIT License。原始版权归 Hermes Web UI Contributors 所有，
详见仓库根目录 [LICENSE](LICENSE)。
