#!/bin/zsh
set -u
zmodload zsh/zselect

SCRIPT_DIR="${0:A:h}"
if [[ -f "$SCRIPT_DIR/build/runtime.env" ]]; then
  source "$SCRIPT_DIR/build/runtime.env" || exit 1
fi
CONFIG_PATH="${TRANSLATOR_CONFIG:-${_TRANSLATOR_INSTALLED_CONFIG:-$SCRIPT_DIR/translator_config.json}}"
if [[ "$CONFIG_PATH" == '~/'* ]]; then
  CONFIG_PATH="$HOME/${CONFIG_PATH#\~/}"
fi
export TRANSLATOR_CONFIG="${CONFIG_PATH:A}"
if [[ -n "${TRANSLATOR_PYTHON:-}" ]]; then
  PYTHON="$TRANSLATOR_PYTHON"
elif [[ -n "${TRANSLATOR_TWO_BATCH_ROOT:-}" ]]; then
  PYTHON="$TRANSLATOR_TWO_BATCH_ROOT/.venv/bin/python"
else
  PYTHON="${_TRANSLATOR_INSTALLED_PYTHON:-$SCRIPT_DIR/../2batch/.venv/bin/python}"
fi
if [[ "$PYTHON" == '~/'* ]]; then
  PYTHON="$HOME/${PYTHON#\~/}"
fi
if [[ "$PYTHON" != /* ]]; then
  PYTHON="${TRANSLATOR_CONFIG:h}/$PYTHON"
fi
# Retain the virtualenv launcher symlink: resolving it would select base Python.
PYTHON="${PYTHON:a}"
if [[ ! -x "$PYTHON" ]]; then
  print -u2 "找不到可执行的 Python：$PYTHON。请重新安装或设置 TRANSLATOR_PYTHON。"
  exit 1
fi
RESULT_APP="$SCRIPT_DIR/build/多段落翻译窗口.app"
RESULT_EXECUTABLE="$RESULT_APP/Contents/MacOS/translation_result_window"
SELECTION_CAPTURE="$SCRIPT_DIR/build/selection_capture"
# One allocation instead of six mktemp subprocesses.
TEMP_BASE="$(mktemp -t paragraph-translator-status)" || exit 1
STATUS_FILE="$TEMP_BASE"
OUTPUT_FILE="$TEMP_BASE.output"
ERROR_FILE="$TEMP_BASE.error"
GESTURE_FILE="$TEMP_BASE.gesture"
READY_FILE="$TEMP_BASE.ready"
STREAM_FILE="$TEMP_BASE.stream"
GESTURE_STATE_DIR="${TMPDIR:-/tmp}/com.zhaoxun.paragraph-translator.shortcut-${UID}"
CAPTURE_LOG="${TRANSLATOR_LOG_DIR:-$HOME/Library/Logs/Translator}/selection_capture.log"
GESTURE_PID=""
TRANSLATION_PID=""
WINDOW_PID=""
exec 3<&0

cleanup_file() {
  local target="${1:-}"
  if [[ -n "$target" && -e "$target" ]]; then
    /bin/unlink "$target" 2>/dev/null || true
  fi
}

cleanup_pending() {
  if [[ -n "$TRANSLATION_PID" ]]; then
    kill -TERM "$TRANSLATION_PID" 2>/dev/null || true
  fi
  if [[ -n "$GESTURE_PID" ]]; then
    # Let the short detector finish and release its shared state.
    wait "$GESTURE_PID" 2>/dev/null || true
  fi
  cleanup_file "$OUTPUT_FILE"
  cleanup_file "$ERROR_FILE"
  cleanup_file "$STATUS_FILE"
  cleanup_file "$GESTURE_FILE"
  cleanup_file "$READY_FILE"
  cleanup_file "$STREAM_FILE"
  cleanup_file "$TEMP_BASE.tmp"
}

trap cleanup_pending EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

"$PYTHON" "$SCRIPT_DIR/shortcut_gesture.py" --state-dir "$GESTURE_STATE_DIR" \
  --ready-file "$READY_FILE" >"$GESTURE_FILE" &
GESTURE_PID=$!
# Only wait for the leader claim, never the 0.45-second format decision.
while [[ ! -s "$READY_FILE" ]]; do
  if ! kill -0 "$GESTURE_PID" 2>/dev/null; then
    print -u2 "无法识别快捷键。"
    exit 1
  fi
  zselect -t 1
done
if [[ "$(<"$READY_FILE")" == "secondary" ]]; then
  exit 0
fi

print -r -- '{"percent":2,"message":"正在读取选中文本…","activate":false}' >"$STATUS_FILE"
: >"$STREAM_FILE"
if [[ ! -x "$RESULT_EXECUTABLE" ]]; then
  print -u2 "找不到翻译窗口，请重新构建。"
  exit 1
fi
# Start the native executable directly, without another LaunchServices process.
"$RESULT_EXECUTABLE" loading "$STATUS_FILE" --stream-file "$STREAM_FILE" >/dev/null 2>&1 &
WINDOW_PID=$!

capture_input() {
  local input=""
  if [[ ! -t 0 ]]; then
    input="$(/bin/cat)"
  fi
  if [[ "$input" != *[^[:space:]]* && -x "$SELECTION_CAPTURE" ]]; then
    /bin/mkdir -p "${CAPTURE_LOG:h}" || return 1
    local -a capture_args=()
    if [[ "${TRANSLATOR_TARGET_PID:-}" == <-> ]]; then
      capture_args+=(--pid "$TRANSLATOR_TARGET_PID")
    fi
    input="$("$SELECTION_CAPTURE" "${capture_args[@]}" 2>"$CAPTURE_LOG")" || true
  fi
  # Capturing has finished; the progress window may now take focus.
  print -r -- '{"percent":3,"message":"正在准备翻译…","activate":true}' >"$STATUS_FILE"
  print -rn -- "$input"
}

# The engine and transport imports warm up while the input is captured.
# Double-press format resolution happens after the network requests finish.
capture_input <&3 | "$PYTHON" "$SCRIPT_DIR/paragraph_translator.py" \
  --status-file "$STATUS_FILE" --gesture-file "$GESTURE_FILE" \
  --stream-file "$STREAM_FILE" \
  >"$OUTPUT_FILE" 2>"$ERROR_FILE" &
TRANSLATION_PID=$!
wait "$TRANSLATION_PID"
TRANSLATION_STATUS=$?
TRANSLATION_PID=""
wait "$GESTURE_PID"
GESTURE_PID=""

RESULT_LAYOUT="multi"
if /usr/bin/grep -Fq '"mode":"词组极速翻译"' "$STATUS_FILE"; then
  RESULT_LAYOUT="phrase"
elif /usr/bin/grep -Fq '"mode":"单段落快速翻译"' "$STATUS_FILE"; then
  RESULT_LAYOUT="single"
elif /usr/bin/grep -Fq '"mode":"多段落综合翻译（保留原段落）"' "$STATUS_FILE"; then
  RESULT_LAYOUT="paragraphs"
fi

if (( TRANSLATION_STATUS == 0 )); then
  COMPLETED_MODE="result"
  PAYLOAD_FILE="$OUTPUT_FILE"
  cleanup_file "$ERROR_FILE"
  ERROR_FILE=""
else
  COMPLETED_MODE="error"
  PAYLOAD_FILE="$ERROR_FILE"
  cleanup_file "$OUTPUT_FILE"
  OUTPUT_FILE=""
fi

# Reuse the already-running window process; publish completion atomically.
# These paths come exclusively from mktemp's fixed safe prefix.
print -r -- "{\"completed\":\"$COMPLETED_MODE\",\"path\":\"$PAYLOAD_FILE\",\"layout\":\"$RESULT_LAYOUT\"}" >"$STATUS_FILE.tmp"
/bin/mv -f "$STATUS_FILE.tmp" "$STATUS_FILE"
if kill -0 "$WINDOW_PID" 2>/dev/null; then
  STATUS_FILE=""
else
  # If the user closed progress, still show the eventual result/error.
  "$RESULT_EXECUTABLE" "$COMPLETED_MODE" "$PAYLOAD_FILE" "$RESULT_LAYOUT" >/dev/null 2>&1 &
fi
if [[ "$COMPLETED_MODE" == "result" ]]; then
  OUTPUT_FILE=""
else
  ERROR_FILE=""
fi
exit "$TRANSLATION_STATUS"
