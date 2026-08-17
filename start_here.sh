#!/usr/bin/env bash
# JAVIP AI STORE - Import 9router v2.0 (Ubuntu/Linux)
# Auto Login OAuth + Bulk Import 9router
set -uo pipefail
cd "$(dirname "$0")"

echo
echo "  ╔════════════════════════════════════════════════╗"
echo "  ║   JAVIP AI STORE - Import 9router v2.0          ║"
echo "  ║   Auto Login OAuth + Bulk Import 9router        ║"
echo "  ╚════════════════════════════════════════════════╝"
echo

# Pick python3 (or python if it's already python3)
PYTHON=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
fi

if [ -z "$PYTHON" ]; then
    echo "  [!] Khong tim thay Python."
    echo "  [!] Cai truoc: sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
    echo
    read -r -p "Nhan Enter de thoat..." _
    exit 1
fi

echo "  [1/5] Kiem tra pip..."
"$PYTHON" -m ensurepip --upgrade >/dev/null 2>&1
if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    echo "  [!] May nay chua co pip hoac pip bi loi."
    echo "      Hay cai: sudo apt install -y python3-pip"
    echo
    read -r -p "Nhan Enter de thoat..." _
    exit 1
fi

# Tao/kich hoat virtualenv rieng cho tool (tranh dung chung Python he thong)
VENV_DIR="$(dirname "$0")/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "  [2/5] Tao virtualenv..."
    "$PYTHON" -m venv "$VENV_DIR"
    if [ $? -ne 0 ]; then
        echo "  [!] Loi khi tao virtualenv."
        echo "      Thu cai: sudo apt install -y python3-venv"
        echo
        read -r -p "Nhan Enter de thoat..." _
        exit 1
    fi
else
    echo "  [2/5] Virtualenv da ton tai, bo qua."
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
PYTHON="$VENV_DIR/bin/python"

echo
echo "  [3/5] Cai/cap nhat pyotp + playwright..."
"$PYTHON" -m pip install --upgrade pip >/dev/null
"$PYTHON" -m pip install --upgrade pyotp playwright
if [ $? -ne 0 ]; then
    echo
    echo "  [!] Loi khi cai pyotp/playwright."
    echo "      Thu chay thu cong: $PYTHON -m pip install --upgrade pyotp playwright"
    echo
    read -r -p "Nhan Enter de thoat..." _
    exit 1
fi

echo
echo "  [4/5] Cai Chromium cho Playwright neu chua co..."
"$PYTHON" -m playwright install chromium
if [ $? -ne 0 ]; then
    echo
    echo "  [!] Loi khi cai Chromium Playwright."
    echo "      Thu chay thu cong: $PYTHON -m playwright install chromium"
    echo
    read -r -p "Nhan Enter de thoat..." _
    exit 1
fi

# Cai dependency he thong cho Chromium tren Linux (can sudo)
if command -v sudo >/dev/null 2>&1; then
    echo "        Cai dependency he thong cho Chromium (can sudo)..."
    sudo "$PYTHON" -m playwright install-deps chromium
else
    echo "        [!] Khong tim thay sudo, bo qua install-deps."
    echo "            Neu Chromium khong chay duoc, tu cai thu cong theo huong dan Playwright."
fi

echo
echo "  [5/5] Dang khoi dong server..."
echo
"$PYTHON" "$(dirname "$0")/server.py"
STATUS=$?
if [ $STATUS -ne 0 ]; then
    echo
    echo "  [!] Server bi loi. Kiem tra log ben tren."
    echo
    read -r -p "Nhan Enter de thoat..." _
fi
