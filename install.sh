#!/bin/bash
# lobster-memory installer
# Builds axolotl_rs (Rust extension) and installs the engine into a managed venv.
set -e

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGED_PYTHON="/Users/sai/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
VENV_DIR="$HOME/.workbuddy/venvs/lobster-memory"
AXOLOTL_URL="https://github.com/LittleLollipop/axolotl.git"
AXOLOTL_DIR="/tmp/axolotl-rs/prototype-rust"

echo "=== lobster-memory installer ==="
echo ""

# ── Step 1: Create persistent venv ──
if [ ! -f "$VENV_DIR/bin/python" ]; then
    echo "[1/5] Creating venv at $VENV_DIR ..."
    "$MANAGED_PYTHON" -m venv "$VENV_DIR"
else
    echo "[1/5] Venv already exists: $VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# ── Step 2: Clone axolotl if needed ──
if [ ! -f "$AXOLOTL_DIR/Cargo.toml" ]; then
    echo "[2/5] Cloning axolotl from GitHub ..."
    mkdir -p /tmp/axolotl-rs
    git clone "$AXOLOTL_URL" /tmp/axolotl-rs 2>/dev/null || true
fi
echo "[2/5] axolotl source: $AXOLOTL_DIR"

# ── Step 3: Install maturin ──
echo "[3/5] Installing maturin ..."
pip install maturin --quiet

# ── Step 4: Build axolotl_rs ──
echo "[4/5] Building axolotl_rs (Rust → Python, ~2 min) ..."
cd "$AXOLOTL_DIR"
maturin develop --release --features python-bindings 2>&1 | tail -3

# ── Step 5: Add skill to Python path ──
echo "[5/5] Adding lobster-memory to Python path ..."
SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])")
echo "$SKILL_DIR" > "$SITE_PACKAGES/lobster_memory.pth"

# ── Verify ──
echo ""
echo "=== Verification ==="
python -c "
import axolotl_rs; print(f'  axolotl_rs: OK')
import sys
sys.path.insert(0, '$SKILL_DIR')
from engine.integration import MemorySession
print(f'  MemorySession: OK')
" 2>&1

echo ""
echo "✓ lobster-memory installed successfully"
echo "  Venv:   $VENV_DIR"
echo "  Python: $VENV_DIR/bin/python"
echo "  Activate: source $VENV_DIR/bin/activate"
echo "  Test:    python -c \"from engine.integration import MemorySession; print('OK')\""
