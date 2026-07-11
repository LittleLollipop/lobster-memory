#!/usr/bin/env bash
# lobster-memory installer
# 构建 axolotl_rs (Rust 扩展) 并把引擎安装到受管 venv。
# 所有路径 / 解释器均可经环境变量覆盖，不绑定作者本机布局。
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── 可覆盖的环境变量（去掉作者本机硬编码）──
PYTHON_BIN="${PYTHON_BIN:-python3}"                         # 用于创建 venv 的 python（需 3.10+）
VENV_DIR="${LOBSTER_VENV_DIR:-$HOME/.workbuddy/venvs/lobster-memory}"
AXOLOTL_URL="${AXOLOTL_URL:-https://github.com/LittleLollipop/axolotl.git}"
AXOLOTL_DIR="${AXOLOTL_DIR:-/tmp/axolotl-build}"            # 源码构建目录（与 clone 目标一致）
WHEELS_DIR="$SKILL_DIR/wheels"                              # 仓库内预编译 wheel 兜底

echo "=== lobster-memory installer ==="
echo "  SKILL_DIR = $SKILL_DIR"
echo "  VENV_DIR  = $VENV_DIR"
echo "  PYTHON    = $PYTHON_BIN"
echo ""

# ── Step 1: 创建持久 venv（用环境变量里的 python）──
if [ ! -f "$VENV_DIR/bin/python" ]; then
  echo "[1/5] 用 $PYTHON_BIN 创建 venv: $VENV_DIR ..."
  "$PYTHON_BIN" -m venv "$VENV_DIR" || { echo "✗ 创建 venv 失败，请确认 $PYTHON_BIN 是 python3.10+" >&2; exit 1; }
else
  echo "[1/5] venv 已存在: $VENV_DIR"
fi
PY="$VENV_DIR/bin/python"
"$PY" -m pip install --quiet --upgrade pip

# ── Step 2: 安装 axolotl_rs（wheel 优先，源码兜底）──
install_axolotl() {
  local installed=0
  # (a) 仓库内预编译 wheel（作者随包提供的同平台 wheel）
  if [ -d "$WHEELS_DIR" ]; then
    local whl
    whl=$(ls "$WHEELS_DIR"/axolotl*.whl 2>/dev/null | head -1)
    if [ -n "$whl" ]; then
      echo "[2/5] 发现仓库内 wheel: $whl，直接安装..."
      "$PY" -m pip install --quiet "$whl" && installed=1
    fi
  fi
  # (b) PyPI 预编译 wheel（需作者发布；失败说明未发布，转源码）
  if [ "$installed" -eq 0 ]; then
    echo "[2/5] 尝试从 PyPI 安装 axolotl_rs (预编译 wheel)..."
    if "$PY" -m pip install --quiet axolotl_rs 2>/dev/null; then
      installed=1
    else
      echo "  (PyPI 上暂无 axolotl_rs 预编译包，转源码构建)"
    fi
  fi
  # (c) 源码构建（要求 Rust 工具链 + maturin）
  if [ "$installed" -eq 0 ]; then
    if ! command -v cargo >/dev/null 2>&1; then
      echo "✗ 源码构建需要 Rust 工具链 (cargo)。" >&2
      echo "  请先安装: https://rustup.rs" >&2
      echo "  或等待作者发布 PyPI 预编译 wheel / 从 releases 下载对应平台 wheel。" >&2
      return 1
    fi
    echo "[2/5] 源码构建 axolotl_rs (Rust → Python, ~2 min)..."
    if [ ! -f "$AXOLOTL_DIR/Cargo.toml" ]; then
      echo "  cloning $AXOLOTL_URL ..."
      git clone "$AXOLOTL_URL" "$AXOLOTL_DIR" || { echo "✗ git clone 失败" >&2; return 1; }
    fi
    "$PY" -m pip install --quiet maturin
    ( cd "$AXOLOTL_DIR" && "$PY" -m maturin develop --release --features python-bindings ) || { echo "✗ maturin 构建失败" >&2; return 1; }
    installed=1
  fi
  return 0
}
if ! install_axolotl; then
  echo "✗ axolotl_rs 安装失败，中断。" >&2
  exit 1
fi

# ── Step 3: 把技能目录加入 venv 的 site-packages (pth) ──
echo "[3/5] 注册 lobster-memory 到 Python path..."
SITE_PACKAGES="$("$PY" -c "import site; print(site.getsitepackages()[0])")"
echo "$SKILL_DIR" > "$SITE_PACKAGES/lobster_memory.pth"

# ── Step 4: 验证导入 ──
echo "[4/5] 验证导入..."
if "$PY" -c "
import axolotl_rs; print(f'  axolotl_rs: OK')
import sys; sys.path.insert(0, '$SKILL_DIR')
from engine.integration import MemorySession
print(f'  MemorySession: OK')
"; then
  echo "✓ 验证通过"
else
  echo "✗ 验证失败" >&2
  exit 1
fi

# ── Step 5: 提示 WorkBuddy 桥接层配置 ──
echo "[5/5] WorkBuddy 桥接层提示..."
WB_PARENT="$(dirname "$SKILL_DIR")"
if [ -f "$WB_PARENT/wb-lobster-memory/runner.py" ]; then
  echo "  检测到相邻 wb-lobster-memory，请在调用 runner.py 时设置:"
  echo "    export LOBSTER_MEMORY_ENGINE=$SKILL_DIR"
  echo "    export LOBSTER_MEMORY_PYTHON=$VENV_DIR/bin/python"
else
  echo "  (可选) 若使用 WorkBuddy，另装同作者 wb-lobster-memory 并指向本引擎:"
  echo "    LOBSTER_MEMORY_ENGINE=$SKILL_DIR"
  echo "    LOBSTER_MEMORY_PYTHON=$VENV_DIR/bin/python"
fi

echo ""
echo "✓ lobster-memory 安装完成"
echo "  Venv:   $VENV_DIR"
echo "  Python: $VENV_DIR/bin/python"
echo "  Activate: source $VENV_DIR/bin/activate"
echo "  Test:    $VENV_DIR/bin/python -c \"from engine.integration import MemorySession; print('OK')\""
