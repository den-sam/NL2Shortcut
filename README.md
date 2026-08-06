# NL2Shortcut

> 自然语言 → 键盘鼠标动作的执行层。说"复制"就按 `Ctrl+C`，说"打开记事本"就 `Win → 搜索 → Enter`。

---

## 设计原理

### 定位：Agent 三档执行中的键盘档

大型 Agent（如 Claude Computer Use）执行操作时有三种方式，按速度/成本降序排列：

```
键盘注入（我们）        API 直调          视觉识别
  < 100ms             < 300ms           1-3s
  0 Token             0 Token          ~1000 Token
```

**NL2Shortcut 占据第一档**——70% 的日常操作本质就是快捷键，无需截图、无需 API、无需 Token。当键盘档不够用时，返回 `fallback_suggested` 让上层 Agent 升级到 API 或视觉档，而不是自行重试。

### 核心管道：本地优先，LLM 兜底

每次执行的决策链路：

```
用户输入 → 意图识别（5 层瀑布）→ 快捷键查找（SQLite）→ 按键注入 → 自检验证
                │
                └── 本地置信度 < 0.75 → 投喂 LLM 补全
```

- **5 层意图识别**：直接关键字（0.95）→ 数据库同义词（0.85）→ 子串匹配（0.80）→ C++ 模糊匹配（0.60）→ spaCy 语义（0.50）
- **本地命中率 ~90%**，简单指令 < 5ms 完成，零 API 调用
- 含"并/和/然后"的多意图输入自动走 LLM 兜底，不会误拆

### 执行引擎：候选键 + 重试 + 自检

查出的快捷键不是单点——每个命令对应一个候选键列表：

```
Copy → [Ctrl+C, Ctrl+Insert]（去重、排序）
```

注入流程为：**注入前快照 → 发送按键 → 注入后验证**，验证失败自动尝试下一个候选键，最多 3 次。

自检不依赖"看屏幕"，而是抓系统状态信号：
- 复制/粘贴类 → 读剪贴板内容变化
- 窗口切换类 → 读前台窗口标题/进程名变化
- 保存类 → 读文件 mtime 变化
- undo/redo/find 等 13 类无可靠信号 → 标记为 noop，不阻塞流程

### 目标分解：Planner 不是"找快捷键"，是"理解意图"

用户的自然语言往往是目标导向而非指令导向：

> "把这段发出去" → 不是找"发送"快捷键，而是生成：
> ```
> 1. 复制 Ctrl+C → 2. 打开邮箱（Win→搜索→Enter）→ 3. 粘贴 Ctrl+V → 4. 发送 Ctrl+Enter
> ```

**Plan 流水线**：语义缓存 → AVR 路由决策 → LLM 推理（注入用户习惯记忆）→ 启发式 Fallback（关键词+正则）

LLM 生成的计划自动存为可复用 YAML 工作流，下次命中直接执行，不再调 API。

### 操作记忆：正向飞轮

每次执行成功后自动记录（app / 动作 / 耗时 / 用户目标），形成闭环：

```
执行 → 记录 → 累积 10 条 → 自动聚类（按 app + 时间窗口切段）
     → 相同签名归组 → 频率达标则生成 OpPattern
     → 高置信度模式自动导出为 YAML 工作流
     → 下一次类似场景主动建议下一步
```

按键规范化是核心——`Ctrl+c`、`Ctrl+C`、`Ctrl+Insert` 统一映射到同一个 canonical key，确保"写法不同但功能相同"的操作被正确合并。

---

## 30 秒上手

```bash
pip install -e .
nl2shortcut exec "复制"
```

输出 `Ctrl+C` 即装好。

## 常用命令

```bash
nl2shortcut exec "复制"              # 单条执行
nl2shortcut exec "粘贴"
nl2shortcut exec "打开记事本"         # Win → 搜索 → Enter 统一流程
nl2shortcut plan "把这段话复制到记事本"  # 目标分解
nl2shortcut master "把这段话复制到记事本" # 确认并执行多步计划
nl2shortcut gui                       # GUI 面板
nl2shortcut start-server              # HTTP API 服务（127.0.0.1:7770）
nl2shortcut suggest                   # 主动建议
nl2shortcut list                      # 列出所有命令
nl2shortcut search 复制               # 搜索命令
nl2shortcut doctor                    # 诊断环境
nl2shortcut stats                     # 执行统计
```

## GUI 面板

```bash
nl2shortcut gui
```

直接输入自然语言执行；侧面导航支持多步自动流程、应用感知上下文、执行历史审计、数据统计。

## Agent 集成（HTTP API）

```python
import urllib.request, json

keys = json.loads(urllib.request.urlopen("http://127.0.0.1:7770/v1/keys").read())
req = urllib.request.Request(
    "http://127.0.0.1:7770/v1/execute",
    data=json.dumps({"intent": "复制", "dry_run": False}).encode(),
    headers={"Content-Type": "application/json"})
result = json.loads(urllib.request.urlopen(req).read())
```

详见 [AGENTS.md](AGENTS.md)。

## 安装

```bash
pip install -e .                   # 核心
pip install -e ".[gui]"            # 含 GUI
pip install -e ".[all]"            # 全部功能（含 C++ 加速、spaCy）
```

LLM 配置（可选，不配也能用 90% 场景）：

```bash
set DEEPSEEK_API_KEY=sk-xxxx
```

## 文档索引

| 想了解什么 | 文档 |
|---|---|
| 完整设计哲学与架构 | [DESIGN.md](docs/DESIGN.md) |
| 常用命令速查 | [common-commands.md](docs/common-commands.md) |
| 多步目标分解 | [multi-step.md](docs/multi-step.md) |
| 操作记忆机制 | [memory.md](docs/memory.md) |
| LLM 配置 | [llm-setup.md](docs/llm-setup.md) |
| Agent 集成接入 | [integration.md](docs/integration.md) |
| 故障排查 | [troubleshooting.md](docs/troubleshooting.md) |

## License

MIT
