#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# venv must live on the native Linux filesystem (not /mnt/c/) to avoid permission issues
VENV_DIR="$HOME/ems-venv"
PYTHON_BIN="python3"

print_header() { echo -e "\n\033[1;36m=== $1 ===\033[0m\n"; }
print_ok()     { echo -e "\033[1;32m[OK]\033[0m $1"; }
print_fail()   { echo -e "\033[1;31m[FAIL]\033[0m $1"; }

# ── 1. System dependencies ──────────────────────────────────────────
print_header "Installing system dependencies"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    build-essential \
    libsndfile1 \
    sox \
    ffmpeg \
    python3-dev \
    python3-venv \
    python3-pip \
    git \
    wget \
    curl
print_ok "System packages installed"

# ── 2. NVIDIA driver / CUDA check ───────────────────────────────────
print_header "Checking NVIDIA GPU access"
if command -v nvidia-smi &>/dev/null; then
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
    print_ok "nvidia-smi is available"
else
    print_fail "nvidia-smi not found. Make sure NVIDIA drivers are installed on Windows."
    print_fail "WSL2 uses the Windows host driver — install the latest Game Ready / Studio driver."
    exit 1
fi

# ── 3. Python virtual environment ───────────────────────────────────
print_header "Setting up Python virtual environment"
if [ ! -d "${VENV_DIR}" ]; then
    ${PYTHON_BIN} -m venv "${VENV_DIR}"
    print_ok "Created venv at ${VENV_DIR}"
else
    print_ok "Venv already exists at ${VENV_DIR}"
fi

source "${VENV_DIR}/bin/activate"
pip install --upgrade pip setuptools wheel

# ── 4. PyTorch with CUDA ────────────────────────────────────────────
print_header "Installing PyTorch with CUDA 12.4"
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124

# ── 5. NeMo + project dependencies ──────────────────────────────────
print_header "Installing NeMo toolkit and project dependencies"
pip install -r "${PROJECT_DIR}/requirements-train.txt"

# ── 6. Verify installation ──────────────────────────────────────────
print_header "Verifying installation"

${PYTHON_BIN} -c "
import torch
print(f'PyTorch : {torch.__version__}')
print(f'CUDA    : {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'GPU     : {torch.cuda.get_device_name(0)}')
    print(f'VRAM    : {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB')
"

${PYTHON_BIN} -c "
import nemo
import nemo.collections.asr as nemo_asr
print(f'NeMo    : {nemo.__version__}')
print('NeMo ASR collections loaded successfully')
"

print_ok "Environment is ready!"
echo ""
echo "Activate the environment with:"
echo "  source ${VENV_DIR}/bin/activate"
echo ""
echo "Next steps:"
echo "  1. python scripts/download_sada.py"
echo "  2. python scripts/prepare_manifests.py"
echo "  3. python scripts/build_tokenizer.py"
echo "  4. python scripts/train_asr.py"
