"""Strip inline chain-of-thought tags out of a streamed answer.

Providers expose model thinking in two different ways:

1. As separate `reasoning` deltas alongside `content` (Groq's openai/gpt-oss-*).
   Easy: the transport keeps them apart for us.
2. Inline in the content itself, wrapped in tags like <thought>...</thought>
   (Google's gemma-4-*, DeepSeek's R1 line and others). The transport has no
   idea, so without this the raw thinking ends up in the answer -- and worse,
   gets fed to the next seat in the debate as though it were the argument.

This is a small state machine rather than a regex because the text arrives in
chunks and a tag can be split across them ("<thou" / "ght>"). It holds back any
trailing fragment that could still turn into a tag, and releases it once it is
clear it cannot.
"""
from __future__ import annotations

OPEN_TAGS = ("<thought>", "<think>", "<thinking>", "<reasoning>")
CLOSE_TAGS = ("</thought>", "</think>", "</thinking>", "</reasoning>")

# Longest tag we might be part-way through at a chunk boundary.
_MAX_TAG = max(len(t) for t in OPEN_TAGS + CLOSE_TAGS)


class ThinkingStripper:
    """Feed it content chunks; get back (answer_text, thinking_text) pairs."""

    def __init__(self) -> None:
        self._buf = ""
        self._in_thought = False

    def feed(self, chunk: str) -> tuple[str, str]:
        self._buf += chunk
        answer: list[str] = []
        thinking: list[str] = []

        while True:
            if self._in_thought:
                idx, tag = _find_first(self._buf, CLOSE_TAGS)
                if idx == -1:
                    # Keep a tail that might be a partial closing tag.
                    safe = max(0, len(self._buf) - (_MAX_TAG - 1))
                    if safe:
                        thinking.append(self._buf[:safe])
                        self._buf = self._buf[safe:]
                    break
                thinking.append(self._buf[:idx])
                self._buf = self._buf[idx + len(tag):]
                self._in_thought = False
            else:
                idx, tag = _find_first(self._buf, OPEN_TAGS)
                if idx == -1:
                    safe = max(0, len(self._buf) - (_MAX_TAG - 1))
                    if safe:
                        answer.append(self._buf[:safe])
                        self._buf = self._buf[safe:]
                    break
                answer.append(self._buf[:idx])
                self._buf = self._buf[idx + len(tag):]
                self._in_thought = True

        return "".join(answer), "".join(thinking)

    def flush(self) -> tuple[str, str]:
        """Release whatever is held back. Call once the stream ends."""
        rest, self._buf = self._buf, ""
        if self._in_thought:
            # Unclosed thought tag: the model never came back. Treat the
            # remainder as thinking rather than leaking it into the answer.
            return "", rest
        return rest, ""


def _find_first(text: str, tags: tuple[str, ...]) -> tuple[int, str]:
    best, best_tag = -1, ""
    for t in tags:
        i = text.find(t)
        if i != -1 and (best == -1 or i < best):
            best, best_tag = i, t
    return best, best_tag
