"""LLM backends for the semantic annotation pass.

Multi-provider like graphify's ``BACKENDS`` table, but far smaller: one
OpenAI-compatible HTTP path (OpenAI, Gemini, DeepSeek, Ollama, any local
gateway), one Anthropic Messages path, and ``claude-cli`` (shells out to the
``claude`` binary using the user's plan — no API key). HTTP uses ``urllib`` so
there is no new dependency.

``detect_backend()`` picks the first backend whose credentials are present,
falling back to ``claude-cli`` when the binary is on PATH, else ``None`` (the
semantic pass is skipped and the AST-only graph still ships).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

TIMEOUT = int(os.environ.get("CODEGRAPH_API_TIMEOUT", "600"))


@dataclass
class Backend:
    name: str
    kind: str            # "openai" | "anthropic" | "claude-cli" | "mock"
    model: str
    base_url: str = ""
    api_key: str = ""


_OPENAI_COMPAT = {
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4.1-mini"),
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai",
               "gemini-2.5-flash"),
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1", "deepseek-chat"),
    "ollama": ("OLLAMA_API_KEY", os.environ.get("OLLAMA_BASE_URL",
               "http://localhost:11434/v1"), os.environ.get("OLLAMA_MODEL",
               "qwen2.5-coder:7b")),
}


def make_backend(name: str) -> Backend | None:
    if name == "none":
        return None
    if name == "mock":
        return Backend("mock", "mock", "mock")
    if name in ("claude-cli", "claude_cli", "cli"):
        if shutil.which("claude"):
            return Backend("claude-cli", "claude-cli",
                           os.environ.get("CODEGRAPH_CLAUDE_MODEL", "claude-sonnet-5"))
        return None
    if name in ("anthropic", "claude"):
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            return None
        return Backend("anthropic", "anthropic",
                       os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
                       os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                       key)
    if name in _OPENAI_COMPAT:
        env_key, base, model = _OPENAI_COMPAT[name]
        key = os.environ.get(env_key, "")
        if not key and name != "ollama":
            return None
        return Backend(name, "openai", os.environ.get(f"{name.upper()}_MODEL", model),
                       base, key or "ollama")
    return None


def detect_backend() -> Backend | None:
    for name in ("anthropic", "openai", "gemini", "deepseek", "ollama", "claude-cli"):
        b = make_backend(name)
        if b is not None:
            return b
    return None


# --------------------------------------------------------------------------- #


class LLMError(RuntimeError):
    pass


def complete(backend: Backend, system: str, user: str, *, max_tokens: int = 8000) -> str:
    """Return the model's text response. Raises :class:`LLMError` on failure."""
    if backend.kind == "mock":
        return _MOCK_HOOK(system, user) if _MOCK_HOOK else "{}"
    if backend.kind == "claude-cli":
        return _claude_cli(backend, system, user)
    if backend.kind == "anthropic":
        return _anthropic(backend, system, user, max_tokens)
    return _openai(backend, system, user, max_tokens)


def _post(url: str, headers: dict, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise LLMError(f"{e.code} {e.read()[:400].decode('utf-8', 'replace')}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(str(e)) from e


def _openai(b: Backend, system: str, user: str, max_tokens: int) -> str:
    data = _post(
        f"{b.base_url.rstrip('/')}/chat/completions",
        {"Authorization": f"Bearer {b.api_key}", "Content-Type": "application/json"},
        {
            "model": b.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
        },
    )
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"unexpected response: {str(data)[:300]}") from e


def _anthropic(b: Backend, system: str, user: str, max_tokens: int) -> str:
    data = _post(
        f"{b.base_url.rstrip('/')}/v1/messages",
        {
            "x-api-key": b.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        {
            "model": b.model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": max_tokens,
        },
    )
    try:
        return "".join(
            part["text"] for part in data["content"] if part.get("type") == "text"
        )
    except (KeyError, TypeError) as e:
        raise LLMError(f"unexpected response: {str(data)[:300]}") from e


def _claude_cli(b: Backend, system: str, user: str) -> str:
    exe = shutil.which("claude") or "claude"
    argv = [
        exe, "-p", user, "--output-format", "json",
        "--model", b.model, "--bare",
        "--append-system-prompt", system,
    ]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if exe.lower().endswith((".cmd", ".bat")):
            argv = ["cmd", "/c", *argv]
    cmd = argv
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT,
            creationflags=flags, encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise LLMError(f"claude cli: {e}") from e
    if proc.returncode != 0:
        raise LLMError(f"claude cli exit {proc.returncode}: {proc.stderr[:300]}")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout
    if out.get("is_error") or out.get("terminal_reason") == "api_error":
        raise LLMError(f"claude cli: {out.get('result') or out.get('terminal_reason')}")
    result = out.get("result", "")
    if not result.strip():
        raise LLMError("claude cli: empty response")
    return result


# test hook — semantic tests set this to a callable(system, user) -> str
_MOCK_HOOK = None


def set_mock(fn) -> None:
    global _MOCK_HOOK
    _MOCK_HOOK = fn


# --------------------------------------------------------------------------- #
# embeddings (opt-in — `codegraph embed`)
# --------------------------------------------------------------------------- #

_EMBED_COMPAT = {
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1", "text-embedding-3-small"),
    "gemini": ("GEMINI_API_KEY",
               "https://generativelanguage.googleapis.com/v1beta/openai",
               "text-embedding-004"),
    "ollama": ("OLLAMA_API_KEY", os.environ.get("OLLAMA_BASE_URL",
               "http://localhost:11434/v1"),
               os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")),
}

_EMBED_MOCK_HOOK = None


def set_embed_mock(fn) -> None:
    global _EMBED_MOCK_HOOK
    _EMBED_MOCK_HOOK = fn


def make_embed_backend(name: str) -> Backend | None:
    if name in ("mock", "hash"):
        return Backend(name, "embed-" + name, name)
    if name in _EMBED_COMPAT:
        env_key, base, model = _EMBED_COMPAT[name]
        key = os.environ.get(env_key, "")
        if not key and name != "ollama":
            return None
        return Backend(name, "embed-openai",
                       os.environ.get(f"{name.upper()}_EMBED_MODEL", model),
                       base, key or "ollama")
    return None


def detect_embed_backend() -> Backend | None:
    for name in ("openai", "gemini", "ollama"):
        b = make_embed_backend(name)
        if b is not None:
            return b
    return None


def _hash_embed(text: str, dim: int = 256) -> list[float]:
    """Dependency-free fallback: hashed character-trigram bag. Captures lexical /
    morphological similarity only (no true synonyms) — honest about its limits."""
    import hashlib
    import math

    v = [0.0] * dim
    t = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    grams = [t[i:i + 3] for i in range(max(0, len(t) - 2))] or [t]
    for g in grams:
        h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "big")
        v[h % dim] += 1.0 if (h >> 8) & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def embed_texts(backend: Backend, texts: list[str], *, batch: int = 128) -> list[list[float]]:
    """Return one vector per input text. Raises :class:`LLMError` on failure."""
    if backend.kind == "embed-mock":
        if _EMBED_MOCK_HOOK:
            return [_EMBED_MOCK_HOOK(t) for t in texts]
        return [_hash_embed(t) for t in texts]
    if backend.kind == "embed-hash":
        return [_hash_embed(t) for t in texts]
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        data = _post(
            f"{backend.base_url.rstrip('/')}/embeddings",
            {"Authorization": f"Bearer {backend.api_key}",
             "Content-Type": "application/json"},
            {"model": backend.model, "input": chunk},
        )
        try:
            rows = sorted(data["data"], key=lambda d: d["index"])
            out.extend(r["embedding"] for r in rows)
        except (KeyError, TypeError) as e:
            raise LLMError(f"unexpected embeddings response: {str(data)[:300]}") from e
    return out
