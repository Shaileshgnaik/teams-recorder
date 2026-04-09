#!/usr/bin/env bash
# setup.sh — One-time setup for Teams Meeting Recorder
#
# Run once:
#   chmod +x setup.sh && ./setup.sh
#
# Then start the app:
#   source venv/bin/activate && python app/main.py

set -e

echo "=== Teams Meeting Recorder — Setup ==="
echo ""

# ── 1. Check Python version ───────────────────────────────────────────────────
PYTHON=$(command -v python3.11 || command -v python3 || echo "")
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.11+ not found."
    echo "Install with: brew install python@3.11"
    exit 1
fi

PYTHON_VERSION=$($PYTHON --version 2>&1 | awk '{print $2}')
echo "✓ Python: $PYTHON_VERSION ($PYTHON)"

# ── 2. Create virtual environment ─────────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo "→ Creating virtual environment..."
    $PYTHON -m venv venv
    echo "✓ Virtual environment created."
else
    echo "✓ Virtual environment already exists."
fi

source venv/bin/activate

# ── 3. Upgrade pip ────────────────────────────────────────────────────────────
echo "→ Upgrading pip..."
pip install --upgrade pip --quiet

# ── 4. Install dependencies ───────────────────────────────────────────────────
echo "→ Installing dependencies (this may take a few minutes)..."
pip install -r requirements.txt

echo "✓ Dependencies installed."

# ── 5. Check for .env file ────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "⚠️  ACTION REQUIRED:"
    echo "   Open .env and add your Anthropic API key:"
    echo "   ANTHROPIC_API_KEY=sk-ant-..."
    echo ""
else
    if grep -q "sk-ant-" .env 2>/dev/null; then
        echo "✓ .env file found with API key."
    else
        echo "⚠️  .env exists but ANTHROPIC_API_KEY may not be set. Check .env"
    fi
fi

# ── 6. Create notes directory ────────────────────────────────────────────────
NOTES_DIR="$HOME/Documents/MeetingNotes"
mkdir -p "$NOTES_DIR"
echo "✓ Notes directory: $NOTES_DIR"

# ── 7. Print next steps ───────────────────────────────────────────────────────
echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env and add: ANTHROPIC_API_KEY=sk-ant-..."
echo "  2. Run the app:"
echo "       source venv/bin/activate"
echo "       python app/main.py"
echo ""
echo "  3. When prompted, grant 'Screen Recording' permission"
echo "     (System Settings → Privacy & Security → Screen Recording)"
echo "     This is required once for capturing Teams system audio."
echo ""
echo "  Note: First transcription downloads ~2GB Parakeet model."
echo "        Subsequent runs use the local cache (~/.cache/huggingface/)."
