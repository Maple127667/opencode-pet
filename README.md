# opencode-pet

一个桌面宠物悬浮窗，使用[月薪喵](https://github.com/Lumi-arta/desktop_cat)的 GIF 精灵（MIT 协议），实时响应 [opencode](https://github.com/sst/opencode) 的会话活动。

桌宠以透明、置顶的 tkinter 窗口运行，由 TUI 插件拉起。插件监听 opencode 的 TUI 本地事件总线，通过 stdin JSON 把活动信号转发给桌宠进程。

## 功能特性

- **9 个独立 GIF 精灵**：idle / waiting / running / running-left / running-right / review / jumping / waving / failed
- **三层频率分离**：120 Hz 物理计算、60 Hz 渲染、12 Hz GIF 推进
- **拖拽抛投物理**：重力 / 弹跳 / 摩擦 / 空气阻力
- **状态感知**：
  - `idle` 待机 → `waiting.gif`，随机 20–120 s 触发 `running-right.gif` 走动
  - `busy` 入场序列 → `running-right.gif`（1.5 s）→ `review.gif`（1.5 s）→ 工作状态
  - `thinking`（reasoning part）→ `review.gif`
  - `speaking`（text part）→ `running.gif`
  - `tool`（tool part）→ `running.gif`
  - 任务完成 → 5 s `waving.gif` 庆祝动画 + 任务统计气泡
  - 重试 / 错误 → `failed.gif`
- **权限 / 提问提醒**：右键单击消除
- **会话时长 / token 统计**：任务结束时气泡显示
- **多实例横向排列**：同时开多个 opencode 窗口时桌宠自动错开，不重叠
- **WS_EX_NOACTIVATE**：窗口永不抢夺终端的键盘焦点

## 架构

```
opencode TUI 进程                      桌宠进程 (pythonw)
────────────────────                   ──────────────────────
index.js (TUI 插件)                    pet.py (tkinter 窗口)
  ├─ api.event.on(...)                   ├─ stdin_loop (线程)
  │   • session.status                   │   解析 JSON 行
  │   • message.part.updated             │   通过 root.after() 派发
  │   • message.updated                  ├─ _effective_state()
  │   • permission/question              │   优先级: alert > flash >
  │   • todo.updated                     │            moving > busy序列 > idle序列
  ├─ client.session.status() 轮询        ├─ 物理步进 @120 Hz
  │   每 500 ms (busy/idle 兜底)         ├─ 渲染 @60 Hz
  └─ stdin JSON ────────────────────►    └─ GIF 推进 @12 Hz
    {type:"status"|"activity"|"flash"|
     "bubble"|"alert"|...}
```

## 安装

### 1. 克隆并注册插件

```bash
git clone https://github.com/Maple127667/opencode-pet.git
```

在 `~/.config/opencode/tui.json` 中注册（Windows 下为 `C:\Users\<用户名>\.config\opencode\tui.json`）：

```json
["file:/path/to/opencode-pet"]
```

如果已有其他插件，追加到数组末尾即可：

```json
["file:/path/to/other-plugin", "file:/path/to/opencode-pet"]
```

### 2. 重启 opencode

桌宠会在屏幕右下角出现。发一条消息，猫会跑进来、审视、说话时跑动，完成后挥手庆祝。

> **可选：更换或调整 GIF**
>
> 仓库中的 GIF 已经预处理过（白底抠成透明）。如果你想替换 GIF 素材或调整抠图阈值，把新的白底 GIF 放进 `gifs/` 目录，然后跑：
>
> ```bash
> python preprocess_gifs.py
> ```
>
> 脚本会从 `-orig.gif` 备份恢复原始白底，再用 flood-fill 算法重新抠图。原始文件始终保留为 `*-orig.gif`。

## 配置

所有可调常量在 `pet.py` 顶部：

| 常量 | 默认值 | 说明 |
|---|---|---|
| `IDLE_WALK_MIN_DELAY` | 20.0 s | 待机走动最小间隔 |
| `IDLE_WALK_MAX_DELAY` | 120.0 s | 待机走动最大间隔 |
| `IDLE_WALK_DURATION` | 1.5 s | 每次走动持续时长 |
| `BUSY_RUN_DURATION` | 1.5 s | busy 入场跑步阶段时长 |
| `BUSY_REVIEW_DURATION` | 1.5 s | busy 审视阶段时长 |
| `GRAVITY` | 4.0 | 拖拽抛投的重力 |
| `AIR_DRAG` | 0.045 | 每步速度衰减（空气阻力）|
| `BOUNCE` | 0.30 | 撞墙能量保留比例 |
| `FRICTION` | 0.55 | 落地后的地面摩擦 |
| `FLOOR_MARGIN` | 60 px | 离屏幕底部的距离（避开任务栏）|
| `PET_GAP` | 20 px | 多实例桌宠之间的横向间隔 |

`index.js` 中：

| 常量 | 默认值 | 说明 |
|---|---|---|
| `STALL_TIMEOUT_MS` | 60000 | 安全网：60 s 无 part.updated 则强制 activity idle |
| 轮询间隔 | 500 ms | `client.session.status()` 兜底频率 |

## 素材来源

[月薪喵 / desktop_cat](https://github.com/Lumi-arta/desktop_cat) — MIT 协议。

## 许可证

MIT
