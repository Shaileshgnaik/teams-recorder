"""
note_generator.py — Generates structured meeting notes from a transcript using Claude API.

Uses Anthropic's claude-sonnet-4-6 model to produce professional, structured meeting notes
in Markdown format from a raw speech transcript. The output includes:
  - Meeting metadata (date, duration)
  - Summary
  - Attendees (inferred from names mentioned in transcript)
  - Topics Discussed
  - Key Decisions
  - Action Items (with owner names where detectable)

Requires ANTHROPIC_API_KEY set in environment (loaded from .env by main.py).
"""

import os
import datetime
from utils import format_duration


# Prompt template — instructs Claude to extract structured information from the transcript.
# We use clear section headers so the output is always consistently formatted.
NOTES_PROMPT = """\
You are a professional meeting notes assistant. Your job is to read a raw meeting transcript \
and produce clean, structured meeting notes in Markdown format.

**Instructions:**
- Be concise but thorough
- Infer attendee names from how people address each other in the transcript
- For Action Items, include the owner's name if mentioned, otherwise write "Owner: TBD"
- If something is unclear from the transcript, omit it rather than guessing
- Use bullet points for lists

**Required sections (use these exact headings):**

## Meeting Summary
3–5 sentences summarizing what the meeting was about and the main outcomes.

## Attendees
Bullet list of names mentioned or inferred from the transcript. If none identifiable, write "Not identified from transcript."

## Topics Discussed
Bullet list of the main topics covered.

## Key Decisions
Bullet list of concrete decisions made. If none, write "No explicit decisions recorded."

## Action Items
Bullet list in format: `- [ ] Description — Owner: Name (Due: date if mentioned)`
If no action items, write "No action items recorded."

---

**Transcript:**
{transcript}
"""


class ClaudeNoteGenerator:
    """
    Generates structured meeting notes from a transcript using Claude claude-sonnet-4-6.

    Usage:
        generator = ClaudeNoteGenerator()
        markdown = generator.generate(transcript="...", duration_seconds=3600)
        # markdown is a complete Markdown document ready to save
    """

    MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    MAX_TOKENS = 2048

    def __init__(self):
        self._client = None  # lazy-initialized on first call

    # ---------------------------------------------------------------------- #
    #  Public API
    # ---------------------------------------------------------------------- #

    def generate(self, transcript: str, duration_seconds: int = 0) -> str:
        """
        Generate structured meeting notes from a transcript.

        Args:
            transcript: Raw text transcript from Parakeet transcription.
            duration_seconds: Length of the meeting in seconds (for metadata header).

        Returns:
            Complete Markdown document as a string, including a metadata header
            (date, duration) prepended to the Claude-generated notes.

        Raises:
            ValueError: If transcript is empty.
            anthropic.APIError: If the Claude API call fails.
        """
        if not transcript or not transcript.strip():
            raise ValueError("Cannot generate notes from empty transcript.")

        self._ensure_client()

        print(f"[notes] Generating meeting notes via Claude {self.MODEL} ...")

        prompt = NOTES_PROMPT.format(transcript=transcript.strip())

        response = self._client.messages.create(
            model=self.MODEL,
            max_tokens=self.MAX_TOKENS,
            messages=[
                {"role": "user", "content": prompt}
            ],
        )

        notes_body = response.content[0].text.strip()
        full_document = self._build_document(notes_body, duration_seconds)

        print(f"[notes] Notes generated. ({len(full_document)} chars)")
        return full_document

    # ---------------------------------------------------------------------- #
    #  Internal helpers
    # ---------------------------------------------------------------------- #

    def _ensure_client(self) -> None:
        """
        Create the Anthropic client if not already created.

        The client reads ANTHROPIC_API_KEY from the environment automatically.
        main.py loads the .env file before this is called, so the key is available.
        """
        if self._client is not None:
            return

        try:
            import anthropic
        except ImportError as e:
            raise ImportError(
                "anthropic package not installed. Install with:\n"
                "  pip install anthropic"
            ) from e

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY not set. Add it to your .env file:\n"
                "  ANTHROPIC_API_KEY=sk-ant-..."
            )

        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        self._client = anthropic.Anthropic(
            api_key=api_key,
            **({"base_url": base_url} if base_url else {}),
        )

    @staticmethod
    def _build_document(notes_body: str, duration_seconds: int) -> str:
        """
        Prepend a YAML-style metadata header to the Claude-generated notes.

        The header captures:
        - Date the meeting was recorded
        - Duration of the recording

        This makes notes easy to search and sort in Obsidian or any Markdown viewer.

        Example output:
        ---
        date: 2026-04-09
        time: 14:30
        duration: 1h 2m 3s
        tags: [meeting, teams]
        ---

        ## Meeting Summary
        ...
        """
        now = datetime.datetime.now()
        duration_str = format_duration(duration_seconds) if duration_seconds > 0 else "unknown"

        header = f"""\
---
date: {now.strftime('%Y-%m-%d')}
time: {now.strftime('%H:%M')}
duration: {duration_str}
tags: [meeting, teams, auto-generated]
---

"""
        return header + notes_body
