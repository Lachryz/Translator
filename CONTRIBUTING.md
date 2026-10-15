# 开发说明

从项目根目录运行命令。Python 3.11 及以上用于引擎、安装器和测试；Swift 原生组件仅支持 macOS。2batch 是单独维护的私有依赖，开发者需要自行取得访问权限，不能假设公开 CI 拥有该源码或有效 API 凭据。

## 代码结构

| 路径 | 职责 |
| --- | --- |
| `paragraph_translator.py` | 拆段、请求编排、校验、流式预览和输出格式化 |
| `translator_api.py` | 公开 Python 接口、结果类型与客户端缓存 |
| `translator_runtime.py` | 配置路径、2batch、凭据文件和 Python 运行环境解析 |
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

复用已有 2batch 环境安装当前项目及开发依赖：

```bash
../2batch/.venv/bin/python -m pip install -e '.[dev]'
```

完整 Python 测试使用模拟传输，不调用真实模型：

```bash
DEEPSEEK_URL=https://example.invalid/v1 DEEPSEEK_KEY=test-only \
  ../2batch/.venv/bin/python -m unittest discover -s tests -v
```

测试运行器为 Python 标准库的 `unittest`，不需要 pytest。缺少 2batch 源码或其依赖时，8 项相关集成测试会明确跳过；其余离线测试仍可运行。公开 CI 的独立单元测试通过，不代表私有依赖集成已验证；应同时查看测试报告中的跳过项。

没有私有后端时，可在独立虚拟环境复现公开 CI 的检查：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
zsh -n run_quick_action.sh
DEEPSEEK_URL=https://example.invalid/v1 DEEPSEEK_KEY=test-only \
  .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python install_quick_action.py --build-only
```

仅验证原生组件编译与本地签名：

```bash
../2batch/.venv/bin/python install_quick_action.py --build-only
```

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

报告验证结果时区分单元测试、离线编译、窗口验证、真实 API 调用和实际快捷键验证。真实调用需要有效凭据，会发送输入文本并计费，不应加入默认测试或公开 CI。
