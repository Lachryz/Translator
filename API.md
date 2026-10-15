# Python API 与命令行

`translator_api.py` 提供进程内调用接口，与桌面共用翻译引擎、配置、模板和校验。调用 API 不读取选区、不操作剪贴板，也不打开窗口。环境及凭据准备见 [README.md](README.md)。

## 最简调用

在本项目根目录运行，或先执行 `python -m pip install -e .` 安装本项目；Python 环境还须包含 2batch 的依赖。

```python
from translator_api import translate

translation = translate(
    "The first paragraph.\nThe second paragraph.",
    preserve_paragraphs=True,
)
print(translation)
```

`preserve_paragraphs` 默认为 `False`，多段结果输出 Markdown 句子列表。需要与桌面单按快捷键相同的段落格式时，显式设为 `True`。只有一个段落时，两种选项的结果格式相同。

## 复用客户端与元数据

循环翻译时，可显式创建并复用一个 `Translator`：

```python
from translator_api import Translator

translator = Translator()

first = translator.translate_text("Market liquidity affects price discovery.")
second = translator.translate(
    "The first paragraph.\nThe second paragraph.",
    preserve_paragraphs=True,
)

print(first)
print(second.translation)
print(second.to_dict())
```

`Translator.translate_text()` 返回 `str`，`Translator.translate()` 返回不可变的 `TranslationResult`。同一实例的顶层调用串行执行；一次多段翻译内部仍按配置并发请求。

也可直接调用 `translate_with_metadata(text, preserve_paragraphs=True)`，取得相同的结果对象：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `translation` | `str` | 通过校验的最终译文 |
| `mode` | `str` | 当前翻译模式的中文名称 |
| `paragraph_count` | `int` | 输入中非空行的数量 |
| `model` | `str` | 配置中的模型名称 |

模式名称包括 `词组极速翻译`、`单段落快速翻译`、`多段落综合翻译` 和 `多段落综合翻译（保留原段落）`。

## 接口与配置

```python
translate(
    text: str,
    *,
    preserve_paragraphs: bool = False,
    config_path: str | Path | None = None,
) -> str

translate_with_metadata(
    text: str,
    *,
    preserve_paragraphs: bool = False,
    config_path: str | Path | None = None,
) -> TranslationResult

Translator(config_path: str | Path | None = None)
get_translator(config_path: str | Path | None = None) -> Translator
clear_translator_cache() -> None
```

配置文件选择顺序为：显式 `config_path`、`TRANSLATOR_CONFIG`、项目根目录的 `translator_config.json`。相对配置路径按当前工作目录解析；配置内的相对路径按配置文件所在目录解析。2batch 目录和凭据文件还可由 `TRANSLATOR_TWO_BATCH_ROOT`、`TRANSLATOR_ENV_FILE` 覆盖。

`translate()`、`translate_with_metadata()` 和 `get_translator()` 共享按已解析配置路径缓存的客户端，最多缓存 4 个；并发首次访问同一路径时只初始化一个引擎。配置内容或路径覆盖变量改变后，需要清除缓存，使下一次调用重新初始化；已有的显式 `Translator` 实例应重新创建：

```python
from translator_api import clear_translator_cache, translate

clear_translator_cache()
result = translate("Market liquidity affects price discovery.")
```

## 输入与错误

- `text` 必须为包含非空白内容的字符串。
- `preserve_paragraphs` 必须为布尔值。
- 每个非空行视为一个段落；行首尾空白会被规范化。
- URL、缩写、引用处理和 Schema 校验沿用桌面引擎。

```python
from translator_api import (
    InvalidTranslationInput,
    TranslationFailed,
    Translator,
)

source_text = "Market liquidity affects price discovery."

try:
    translator = Translator()
    result = translator.translate_text(source_text)
except InvalidTranslationInput as exc:
    print(f"输入错误：{exc}")
except TranslationFailed as exc:
    print(f"翻译失败：{exc}")
```

`InvalidTranslationInput` 表示输入不满足接口约束。`TranslationFailed` 统一包装配置读取、后端初始化和实际翻译阶段的错误，原始异常可通过 `__cause__` 查看。顶层便捷函数会先校验文本和排版参数，再创建客户端，因此无效输入不会触发后端初始化。

一次调用会发送真实模型请求并产生费用。接口不会自动重试整个 `translate()`；引擎内部仍会执行传输重试、Schema 重试和必要的片段回退。普通重试只重提失败段落，最终校验失败则抛出错误。

## 命令行

从项目根目录执行：

```bash
printf '%s' 'The first paragraph.
The second paragraph.' | \
  ../2batch/.venv/bin/python paragraph_translator.py --preserve-paragraphs
```

成功结果只写入标准输出，错误写入标准错误并返回退出码 `1`。调用方应检查退出码；不加 `--preserve-paragraphs` 时，多段结果使用 Markdown 列表。

使用其他配置：

```bash
printf '%s' 'Market liquidity affects price discovery.' | \
  ../2batch/.venv/bin/python paragraph_translator.py --config /path/to/config.json
```

`--status-file`、`--stream-file` 和 `--gesture-file` 用于桌面启动器的进度、增量事件和排版协作，普通脚本调用不需要传入。`paragraph_translator.py --help` 可离线查看完整参数，不发送模型请求。
