#!/usr/bin/env bash
# setup.sh — One-time setup for Teams Meeting Recorder
#
# Requirements:
#   - Apple Silicon Mac (M1/M2/M3 or later) — mlx-whisper requires Apple MLX framework
#   - macOS 14 Sonoma or later
#   - Python 3.11 or 3.12  (brew install python@3.12)
#   - Microsoft Teams v2 installed
#
# Run once:
#   chmod +x setup.sh && ./setup.sh
#
# Then start the app:
#   source venv/bin/activate && python app/main.py

set -e

echo "=== Teams Meeting Recorder — Setup ==="
echo ""

# ── 1. Check Apple Silicon ────────────────────────────────────────────────────
ARCH=$(uname -m)
if [ "$ARCH" != "arm64" ]; then
    echo "ERROR: This app requires Apple Silicon (M1/M2/M3)."
    echo "mlx-whisper only runs on Apple MLX framework (arm64)."
    exit 1
fi
echo "✓ Architecture: $ARCH (Apple Silicon)"

# ── 2. Check Python version ───────────────────────────────────────────────────
PYTHON=$(command -v python3.12 || command -v python3.11 || command -v python3 || echo "")
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.11+ not found."
    echo "Install with: brew install python@3.12"
    exit 1
fi

PYTHON_VERSION=$($PYTHON --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]; }; then
    echo "ERROR: Python 3.11+ required (found $PYTHON_VERSION)."
    echo "Install with: brew install python@3.12"
    exit 1
fi

echo "✓ Python: $PYTHON_VERSION ($PYTHON)"

# ── 3. Create virtual environment ─────────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo "→ Creating virtual environment..."
    $PYTHON -m venv venv
    echo "✓ Virtual environment created."
else
    echo "✓ Virtual environment already exists."
fi

source venv/bin/activate

# ── 4. Upgrade pip ────────────────────────────────────────────────────────────
echo "→ Upgrading pip..."
pip install --upgrade pip --quiet

# ── 5. Install dependencies ───────────────────────────────────────────────────
echo "→ Installing dependencies (this may take a few minutes)..."
pip install -r requirements.txt
echo "✓ Dependencies installed."

# ── 6. Check for .env file ────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "⚠️  ACTION REQUIRED:"
    echo "   Open .env and add your Anthropic API key:"
    echo "   ANTHROPIC_API_KEY=sk-ant-..."
    echo ""
else
    if grep -qv "your-key-here" .env 2>/dev/null && grep -q "ANTHROPIC_API_KEY" .env 2>/dev/null; then
        echo "✓ .env file found."
    else
        echo "⚠️  .env exists but ANTHROPIC_API_KEY may not be set. Check .env"
    fi
fi

# ── 7. Create notes directory ─────────────────────────────────────────────────
NOTES_DIR="$HOME/Documents/MeetingNotes"
mkdir -p "$NOTES_DIR"
echo "✓ Notes directory: $NOTES_DIR"

# ── 8. Print next steps ───────────────────────────────────────────────────────
echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env and set your API key:"
echo "       ANTHROPIC_API_KEY=sk-ant-..."
echo ""
echo "  2. Start Microsoft Teams (required for call detection)"
echo ""
echo "  3. Run the app:"
echo "       source venv/bin/activate"
echo "       python app/main.py"
echo ""
echo "  4. On first recording, macOS will prompt for Microphone permission."
echo "     Grant it in System Settings → Privacy & Security → Microphone."
echo ""
echo "  Note: First transcription downloads the mlx-whisper model (~145 MB)"
echo "        from HuggingFace to ~/.cache/huggingface/."
echo "        Subsequent runs use the local cache — no internet needed."
echo ""
echo "  No Screen Recording or Speech Recognition permission required."
