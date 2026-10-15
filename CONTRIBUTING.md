# 开发说明

从项目根目录运行命令。使用 uv 管理项目 `.venv` 和锁定依赖；Python 3.11 及以上用于引擎、安装器和测试，Swift 原生组件仅支持 macOS。Translator 的内置请求层位于 `translator_transport/`，开发与公开 CI 无需持有有效 API 凭据。

## 代码结构

| 路径 | 职责 |
| --- | --- |
| `paragraph_translator.py` | 拆段、请求编排、校验、流式预览和输出格式化 |
| `translator_api.py` | 公开 Python 接口、结果类型与客户端缓存 |
| `translator_runtime.py` | 配置路径、凭据文件和 Python 运行环境解析 |
| `translator_transport/` | 内置的 DeepSeek 流式传输、并发、重试与输出校验 |
| `translation_policy/` | 缩写、URL 保护、片段回退和窄范围语义检查 |
| `prompts/` | JSON 格式的 Jinja2 提示词模板 |
| `schemas/` | 模型输出 JSON Schema |
| `hotkey_daemon.swift` | 全局快捷键监听及来源应用记录 |
| `selection_capture.swift` | 读取选区，无选区时按需复制 |
| `run_quick_action.sh` | 一次桌面调用、进程协调与临时文件清理 |
| `shortcut_gesture.py` | 0.45 秒单按、双按识别 |
| `result_window.swift` | 原生进度、流式译文与最终结果窗口 |
| `install_quick_action.py` | 编译、签名、macOS 服务及常驻进程安装 |
| `tests/` | Python 单元测试和 Swift 窗口验证辅助源码 |

## 本地验证

安装锁定的开发依赖，复现公开 CI 的检查：

```bash
uv sync --locked --group dev
uv run --locked ruff check .
zsh -n run_quick_action.sh
DEEPSEEK_URL=https://example.invalid/v1 DEEPSEEK_KEY=test-only \
  uv run --locked python -m unittest discover -s tests -v
uv run --locked python install_quick_action.py --build-only
```

测试运行器为 Python 标准库的 `unittest`，不需要 pytest。内置请求层和翻译引擎的集成测试始终运行；HTTP 响应与流式事件由测试模拟，不调用真实模型。测试凭据是无效占位符，不需要读取本地密钥。

`--build-only` 不安装系统服务或常驻进程，也不检查辅助功能权限和物理快捷键。正式安装命令会更新用户级 Services 和 LaunchAgents；验证构建时请使用 `--build-only`。

修改窗口时，还应检查首段流式输出、重试替换、长文本滚动、最终结果、错误和提前关闭窗口等行为。`tests/window_smoke.swift` 与 `tests/window_height_smoke.swift` 是需要组合结果窗口源码运行的辅助程序，不是独立 Swift 测试包。

## 修改约束

- 保持选区优先、无选区才复制、只接受新复制内容的输入规则。
- 保持单按保留段落、双按输出列表的桌面行为，以及 Python API 默认输出列表的兼容性。
- 确定性的拆段、ID、URL、引用和排版逻辑使用代码实现。
- 提示词使用 JSON 结构与 Jinja2 模板；模板初始化后复用，每次请求按实际参数渲染。
- 模型输出必须经过 Schema 校验；重试附带结构化错误，仅重试失败段落。
- 流式预览不能暴露内部占位符或模型思考，最终结果必须由校验后的内容替换。
- 不提交 `.env`、API 密钥、个人输入、日志、缓存和 `build/` 产物。示例配置只使用占位符。
- 配置和启动器不得依赖某位开发者的绝对目录；安装器生成的本地运行路径留在忽略的构建目录。
- 依赖变更同时更新 `pyproject.toml` 与 `uv.lock`；正常安装和 CI 使用 `--locked`，避免隐式更改解析结果。
- 内置请求层只保留 Translator 使用的流式请求、并发、重试和校验能力。

报告验证结果时区分单元测试、离线编译、窗口验证、真实 API 调用和实际快捷键验证。真实调用需要有效凭据，会发送输入文本并计费，不应加入默认测试或公开 CI。
