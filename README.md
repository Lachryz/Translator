# Translator

macOS 英文转中文工具。选中文本，按 `Control+Option+Command+T`，即可在原生窗口查看流式译文；完成后，最终译文自动复制到剪贴板。也可通过 Python API 或标准输入调用同一翻译引擎。

默认使用 `deepseek-flash`，侧重会计、金融、经济与计量领域的学术表达。Translator 内置请求层，负责流式传输、并发与重试；模型输出经过 JSON Schema 和引用、缩写、URL 等约束校验。

## 环境要求

- macOS，已安装 Xcode Command Line Tools，可执行 `xcrun swiftc`。
- Python 3.11 或更新版本。
- [uv](https://docs.astral.sh/uv/)：创建项目虚拟环境并按锁文件安装依赖。
- 有权调用所配置模型的 DeepSeek API 密钥。

## 安装

克隆项目并创建独立的 `.venv`：

```bash
git clone https://github.com/Lachryz/Translator.git
cd Translator
uv sync --locked
cp -n .env.example .env
```

在项目根目录的 `.env` 中填写 API 密钥；如果已有该文件，直接编辑，不要覆盖现有凭据：

```dotenv
DEEPSEEK_KEY=<你的 API 密钥>
```

只有 `DEEPSEEK_KEY` 必填。接口地址默认使用 `https://api.deepseek.com`，模型和其他参数可保持默认；只有需要其他 OpenAI 兼容地址时，才在 `.env` 中添加 `DEEPSEEK_URL` 覆盖。项目提供 [.env.example](.env.example) 作为格式示例，不包含有效凭据。默认读取项目根目录的 `.env`，也可通过配置指定其他凭据文件。`.env` 和本机构建文件已列入忽略规则；真实密钥只应保存在本地凭据文件或环境变量中，不应写入源码、示例或文档，也不应提交到 Git。

先只构建和签名原生组件，检查编译环境：

```bash
uv run --locked python install_quick_action.py --build-only
```

该命令不安装系统服务，也不注册常驻进程。确认构建成功后，执行正式安装：

```bash
uv run --locked python install_quick_action.py
```

安装器先在临时目录完成 Swift 编译和本地签名，再替换构建产物。正式安装会创建或更新：

| 位置 | 用途 |
| --- | --- |
| `build/Translator Hotkey.app` | 全局快捷键与辅助功能权限主体 |
| `build/selection_capture` | 选区读取与按需自动复制 |
| `build/多段落翻译窗口.app` | 原生进度、译文与错误窗口 |
| `~/Library/Services/多段落翻译.workflow` | macOS 服务入口 |
| `~/Library/LaunchAgents/com.zhaoxun.paragraph-translator.hotkey.plist` | 登录后常驻热键进程 |

首次使用时，在“系统设置 → 隐私与安全性 → 辅助功能”中允许 `Translator Hotkey.app`。安装后移动项目目录、切换 Python 环境或修改安装时使用的配置位置，需要重新运行安装器。

## 使用规则

| 操作或输入 | 行为 |
| --- | --- |
| 单按快捷键 | 翻译当前选区；多段结果保留段落 |
| 0.45 秒内连按两次 | 多段结果按中文句号整理为 Markdown 列表；第二次按键不重复请求 |
| 短英文词组 | 跳过全文语境分析，输出上限最多 128 tokens |
| 单段文本 | 直接翻译 |
| 多段文本 | 先生成全文语境，再并发翻译各段，按原顺序输出 |

每个非空行视为一个段落，空行不单独形成段落。词组和单段结果不受多段排版选项影响。

输入优先级为：明确传入的标准输入、前台应用通过辅助功能接口提供的选区、向同一前台应用发送一次 `Command+C`。自动复制只在没有选区时发生，并且只接受此次复制新产生的非空内容。前台应用变化、复制超时或剪贴板未变化时，均报告无输入，不读取旧剪贴板。

窗口在捕获输入时保持来源应用焦点，捕获完成后才激活。词组、单段和单按多段结果在同一窗口中流式显示；后面的段落提前完成时先缓冲，确保顺序不变。正文到达后隐藏进度条，窗口保持固定宽度，随内容增高，达到屏幕可用高度后纵向滚动。双按多段模式完成后显示列表。提前关闭进度窗口后，完成时仍会打开最终结果。

流式内容是尚未完成校验的预览，重试时可能被替换；复制到剪贴板的是通过校验的最终译文。

## 配置

默认配置见 [translator_config.json](translator_config.json)。相对路径以配置文件所在目录为基准。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `deepseek_url` | `https://api.deepseek.com` | API 基础地址，可由 `DEEPSEEK_URL` 覆盖 |
| `model` | `deepseek-flash` | 模型名称 |
| `temperature` | `1` | 采样温度 |
| `max_tokens` | `4096` | 普通翻译请求输出上限 |
| `seed` | `42` | 请求种子 |
| `concurrency` | `100` | 内嵌请求层的并发上限 |
| `validation_retries` | `2` | 模型输出校验失败后的重试次数 |
| `prompt_token_limit` | `64000` | 请求提示词 token 限制 |
| `phrase_word_limit` | `8` | 词组模式英文词数上限 |
| `env_file` | `".env"` | 凭据文件；`null` 或省略时使用配置文件目录下的 `.env` |

支持以下环境变量：

| 环境变量 | 用途 |
| --- | --- |
| `DEEPSEEK_KEY` | 必填的 API 密钥 |
| `DEEPSEEK_URL` | 可选的 API 基础地址覆盖 |
| `TRANSLATOR_CONFIG` | 指定配置文件；CLI 的 `--config` 和 Python API 的显式 `config_path` 优先 |
| `TRANSLATOR_ENV_FILE` | 覆盖凭据文件路径 |
| `TRANSLATOR_PYTHON` | 覆盖桌面启动器使用的 Python 解释器，默认使用项目 `.venv/bin/python` |
| `TRANSLATOR_LOG_DIR` | 覆盖选区与自动复制日志目录 |

桌面启动器会复用 `build/runtime.env` 中记录的配置和 Python 路径，该文件不含凭据。更换解释器后可重新执行 `--build-only` 刷新缓存；更换项目位置或服务入口仍需正式安装。安装器也支持 `--config /path/to/config.json`。终端中临时设置的变量不会自动进入已运行的常驻进程；日常配置宜保存在配置文件中。

## 命令行与 Python API

命令行只读取标准输入，成功译文写入标准输出，错误写入标准错误并返回非零退出码：

```bash
printf '%s' 'Market liquidity affects price discovery.' | \
  uv run --locked paragraph-translator --preserve-paragraphs
```

Python 调用：

```python
from translator_api import translate

result = translate(
    "The first paragraph.\nThe second paragraph.",
    preserve_paragraphs=True,
)
print(result)
```

**CLI 和 Python API 默认将多段结果输出为 Markdown 列表；桌面单按默认保留段落。** 脚本中需要段落格式时，请显式设置 `preserve_paragraphs=True` 或传入 `--preserve-paragraphs`。复用客户端、元数据、缓存与异常说明见 [API.md](API.md)。

## 翻译与数据边界

提示词采用 JSON 结构和 Jinja2 模板；确定性的拆段、链接地址保护、缩写还原、引用回填和格式化由代码完成。校验失败会把结构化错误加入下一次重试；仅重试失败段落，必要时切换到由代码拼接受保护片段的结构化回退。最终仍未通过校验的结果会报告错误。

策略要求保留原文的否定、限定、年份和关系方向，并统一常见学术术语。少量确定性语义检查不能覆盖全部翻译错误，正式使用仍应核对原文。缩写只在原文定义或明确专业语境支持时展开；Markdown 链接仅翻译标题，目标地址保持不变。策略来源见 [translation_policy/UPSTREAM.md](translation_policy/UPSTREAM.md)。

**真实翻译会把所选文本发送到所配置的外部 API，并产生相应费用。** 多段翻译包含全文语境请求和逐段请求，失败重试也可能增加调用次数。离线单元测试和 `--build-only` 不验证真实 API 可用性、翻译质量或物理快捷键权限。

## 开发与诊断

先安装开发依赖，再运行离线测试。测试覆盖内置请求层与翻译引擎的集成，使用模拟传输，不调用真实模型，也不需要本地密钥：

```bash
uv sync --locked --group dev
DEEPSEEK_URL=https://example.invalid/v1 DEEPSEEK_KEY=test-only \
  uv run --locked python -m unittest discover -s tests -v
```

公开 CI 同样执行完整离线测试，包括请求层与翻译引擎的集成测试。项目结构、修改约束和验证范围见 [CONTRIBUTING.md](CONTRIBUTING.md)。

- 热键及权限日志：`~/Library/Logs/Translator/hotkey.log`
- 选区与复制日志：`~/Library/Logs/Translator/selection_capture.log`

选区诊断日志只记录权限、状态、进程和字节数，不记录选中的正文。热键无响应时，先检查辅助功能权限和上述日志；提示找不到 Python 或构建产物时，检查配置并重新运行安装器。接口认证失败时，检查 `.env` 的地址、密钥和模型权限。
