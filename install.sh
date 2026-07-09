#!/bin/bash
# lobster-memory install script
# Prebuilt wheel preferred; falls back to maturin source build.
set -e

echo "=== lobster-memory installer ==="

# ── Try prebuilt wheel first ──
WHEEL_DIR="$(dirname "$0")/wheels"
if ls "$WHEEL_DIR"/lobster_memory*.whl 2>/dev/null; then
    echo "Found prebuilt wheel, installing..."
    pip install "$WHEEL_DIR"/lobster_memory*.whl
    echo "✓ lobster-memory installed from wheel"
    python -c "import lobster_memory; print(f'  version={lobster_memory.__version__}')"
    exit 0
fi

# ── Check for axolotl source ──
AXOLOTL_DIR="${AXOLOTL_DIR:-/tmp/axolotl-rs/prototype-rust}"
if [ ! -d "$AXOLOTL_DIR" ]; then
    echo "Cloning axolotl from GitHub..."
    git clone https://github.com/LittleLollipop/axolotl.git /tmp/axolotl-rs
    AXOLOTL_DIR="/tmp/axolotl-rs/prototype-rust"
fi

# ── Build from source ──
if command -v cargo &>/dev/null; then
    echo "Building axolotl_rs with maturin..."
    cd "$AXOLOTL_DIR"
    pip install maturin
    maturin develop --release --features python-bindings
    echo "✓ axolotl_rs built and installed"
else
    echo ""
    echo "ERROR: Rust toolchain not found."
    echo "Install Rust: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
    echo "Then re-run this script."
    exit 1
fi

# ── Install lobster-memory engine ──
ENGINE_DIR="$(dirname "$0")/engine"
if [ -f "$ENGINE_DIR/requirements.txt" ]; then
    pip install -r "$ENGINE_DIR/requirements.txt"
fi

echo ""
echo "✓ lobster-memory installed successfully"
echo "  Test: python -c 'from lobster_memory import LobsterMemory; lm=LobsterMemory(\"test.axeb\"); print(lm)'"
