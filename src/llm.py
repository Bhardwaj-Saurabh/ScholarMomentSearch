"""Multimodal LLM — cited answer synthesis from frames, per-tenant switchable.

Every call takes an LLMConfig. Where it comes from (resolved in
src/rag/search.py):
  1. the user's own hosted model (ms_user_llms row — a vLLM/Ollama/LM Studio/
     Together/OpenRouter endpoint via base_url, NVIDIA NIM, or Anthropic), or
  2. the server-wide LLM_* env config as the fallback.

The two multimodal calls are where latency and cost actually live (retrieval
is milliseconds), so frames are downscaled to LLM_IMAGE_MAX_PX before they are
sent and only TOP_K of them ever reach the model.

Providers:
  * "openai"    — Chat Completions; covers every OpenAI-compatible server
                  (vLLM, Ollama, LM Studio, Together, Groq, OpenRouter, ...)
                  via base_url.
  * "nvidia"    — NVIDIA NIM / build.nvidia.com hosted vision models.
                  OpenAI-compatible, same client with NVIDIA's endpoint.
  * "anthropic" — the Anthropic Messages API.

Provider SDKs are imported lazily — only the one you use.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from . import config, metrics

# NVIDIA's hosted inference endpoint (OpenAI-compatible).
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

PROVIDERS = ("openai", "nvidia", "anthropic")

SYSTEM = (
    "You answer a user's question using the numbered moments provided as your "
    "evidence. Each moment is a specific excerpt from ONE named source (a video, "
    "paper, or slide deck) and shows that source title. A moment may include a "
    "FRAME (what was shown on screen) and/or an EXCERPT of text (what was said "
    "aloud in a video, or a passage from a paper/deck page or slide). Use BOTH "
    "kinds of evidence: for a question about what was said or what a document "
    "states, read the excerpt text; for a question about what is shown, read "
    "the frame.\n"
    "Rules:\n"
    "1. Read the question carefully and answer exactly what is asked. Start with a "
    "one-line direct answer, then explain in short paragraphs — ONE paragraph per "
    "distinct point. Keep it focused, don't pad. No preamble, don't restate the "
    "question.\n"
    "2. Ground every claim in the moments and cite the moment number(s) in square "
    "brackets, e.g. [1] or [2, 3]. Quote excerpt text accurately — keep the actual "
    "wording, names, and numbers exactly as given, don't alter, round, or "
    "extrapolate them.\n"
    "3. Group the relevant moments by the point they make:\n"
    "   - Moments that make the SAME point (especially several from the same "
    "source) belong TOGETHER in ONE paragraph, cited together, e.g. [1, 2]. Do not "
    "split one shared point across separate paragraphs.\n"
    "   - Moments that make DIFFERENT points, or come from different sources, go "
    "in SEPARATE paragraphs, each with its own citation.\n"
    "   Cover every distinct relevant point — don't merge unrelated ones and don't "
    "drop any.\n"
    "4. Don't use outside knowledge or invent details that aren't in the moments. "
    "Refer to sources ONLY by their bracket citation [n] — never write out a "
    "paper, talk, or deck's name/title in your answer text, even one shown in "
    "the moments. Write \"[3] found that...\" or \"the retrieved excerpt [3] "
    "shows...\", never \"the CLIP paper found...\" or \"the Mamba paper "
    "shows...\". This matters most for a source the question names but the "
    "moments DON'T actually contain: without naming sources yourself, you "
    "cannot accidentally present a different, unrelated moment as if it were "
    "evidence about a work that was never retrieved.\n"
    "5. Abstain if none of the moments are actually about what the question asks — "
    "being topically adjacent (e.g. both are machine-learning papers) does not "
    "make a moment relevant to a specific named paper, statistic, or claim it "
    "doesn't contain. When unsure whether a moment truly supports a specific "
    "claim, say so rather than stating it as fact. If at least one moment "
    "genuinely does address the question, answer from it fully — do not refuse "
    "just because the match is partial."
)


@dataclass
class LLMConfig:
    provider: str = "openai"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = 1024


def env_config() -> LLMConfig | None:
    """The server-wide fallback model from LLM_* env vars, if configured."""
    if not config.llm_configured():
        return None
    return LLMConfig(provider=config.LLM_PROVIDER, model=config.LLM_MODEL,
                     api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL,
                     max_tokens=config.LLM_MAX_TOKENS)


def from_row(row: dict) -> LLMConfig:
    """A tenant's own hosted model (ms_user_llms row)."""
    return LLMConfig(provider=row.get("provider") or "openai",
                     model=row.get("model") or "",
                     api_key=row.get("api_key") or "",
                     base_url=row.get("base_url") or "",
                     max_tokens=config.LLM_MAX_TOKENS)


def _intro(question: str, n: int) -> str:
    return (
        f"QUESTION: {question}\n\n"
        f"Answer this question using the {n} moments below (numbered 1 to {n}). "
        "Each shows its source title and has a timestamp/locator and a frame "
        "and/or excerpt text. If the question is about what was said or what a "
        "document states, use the excerpt text. Give a direct answer grounded "
        "in the relevant moment(s), cited as [n]. Only answer about a specific "
        "named source if one of the moments' source titles actually matches it. "
        "Only say you couldn't find it if none of the moments genuinely address "
        "the question."
    )


def _downscale(jpeg: bytes) -> bytes:
    """Shrink a frame before it becomes LLM image tokens."""
    from PIL import Image

    img = Image.open(io.BytesIO(jpeg))
    if max(img.size) <= config.LLM_IMAGE_MAX_PX:
        return jpeg
    img.thumbnail((config.LLM_IMAGE_MAX_PX, config.LLM_IMAGE_MAX_PX))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def answer(question: str, moments: list[dict], cfg: LLMConfig, *, kind: str = "answer") -> str:
    """Synthesize a cited answer from retrieved moments with `cfg`'s model.

    moments: [{"image": bytes|None, "transcript": str|None, "timestamp": str}]
    — each may carry a frame, a transcript excerpt, or both.

    kind (DESIGN.md §3c component 18): tags the metrics.record_llm_usage()
    call this call site ends up making — "answer" (the default, a real cited
    answer), or "caption"/"ping" for the other callers below that reuse this
    same function for a different task. Only "answer" counts toward the
    "LLM answers" stat; every kind's tokens/cost still accumulate."""
    if cfg.provider == "anthropic":
        return _answer_anthropic(cfg, question, moments, kind)
    return _answer_openai(cfg, question, moments, kind)


def caption_image(image_jpeg: bytes, cfg: LLMConfig) -> str:
    """One-line caption for a slide with too little extractable text to embed
    on its own (DESIGN.md component 4's deck captioning step). Reuses answer()'s
    provider dispatch exactly like ping() does, rather than a parallel code path."""
    return answer(
        "Describe this presentation slide in one or two sentences: what claim, "
        "diagram, or result does it show? Be concrete and specific.",
        [{"image": image_jpeg, "transcript": None, "timestamp": ""}],
        cfg, kind="caption",
    )


def ping(cfg: LLMConfig) -> str:
    """Connectivity + vision check: one tiny image, one word back. Raises with
    the provider's error on failure (surfaced to the settings UI)."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (220, 40, 40)).save(buf, format="JPEG")
    return answer("Reply with the dominant color of moment 1, one word.",
                  [{"image": buf.getvalue(), "transcript": None, "timestamp": "00:00"}],
                  cfg, kind="ping")


def _base_url(cfg: LLMConfig) -> str | None:
    if cfg.base_url:
        return cfg.base_url
    if cfg.provider == "nvidia":
        return NVIDIA_BASE_URL
    return None


def _label(i: int, m: dict) -> str:
    line = f"[{i}] @ {m.get('timestamp', '')}"
    if m.get("source"):
        line += f' from "{m["source"]}"'
    if m.get("transcript"):
        line += f' — excerpt: "{m["transcript"]}"'
    if m.get("image") is None:
        line += " (text only, no frame)"
    return line


def _answer_openai(cfg: LLMConfig, question: str, moments: list[dict], kind: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=cfg.api_key or "not-needed", base_url=_base_url(cfg))
    content: list[dict] = [{"type": "text", "text": _intro(question, len(moments))}]
    for i, m in enumerate(moments, 1):
        content.append({"type": "text", "text": _label(i, m)})
        if m.get("image"):
            uri = f"data:image/jpeg;base64,{base64.b64encode(_downscale(m['image'])).decode()}"
            content.append({"type": "image_url", "image_url": {"url": uri}})
    resp = client.chat.completions.create(
        model=cfg.model,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": content}],
        temperature=0.2,
        max_tokens=cfg.max_tokens,
    )
    usage = getattr(resp, "usage", None)
    if usage:
        metrics.record_llm_usage(cfg.model, usage.prompt_tokens or 0,
                                 usage.completion_tokens or 0, kind=kind)
    return (resp.choices[0].message.content or "").strip()


def _answer_anthropic(cfg: LLMConfig, question: str, moments: list[dict], kind: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=cfg.api_key, base_url=cfg.base_url or None)
    blocks: list[dict] = [{"type": "text", "text": _intro(question, len(moments))}]
    for i, m in enumerate(moments, 1):
        blocks.append({"type": "text", "text": _label(i, m)})
        if m.get("image"):
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": base64.b64encode(_downscale(m["image"])).decode()}})
    resp = client.messages.create(
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        system=SYSTEM,
        messages=[{"role": "user", "content": blocks}],
    )
    usage = getattr(resp, "usage", None)
    if usage:
        metrics.record_llm_usage(cfg.model, usage.input_tokens or 0,
                                 usage.output_tokens or 0, kind=kind)
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _complete_openai(cfg: LLMConfig, system: str, prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=cfg.api_key or "not-needed", base_url=_base_url(cfg))
    resp = client.chat.completions.create(
        model=cfg.model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=cfg.max_tokens,
    )
    usage = getattr(resp, "usage", None)
    if usage:
        metrics.record_llm_usage(cfg.model, usage.prompt_tokens or 0,
                                 usage.completion_tokens or 0, kind="complete")
    return (resp.choices[0].message.content or "").strip()


def _complete_anthropic(cfg: LLMConfig, system: str, prompt: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=cfg.api_key, base_url=cfg.base_url or None)
    resp = client.messages.create(
        model=cfg.model, max_tokens=cfg.max_tokens, system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    usage = getattr(resp, "usage", None)
    if usage:
        metrics.record_llm_usage(cfg.model, usage.input_tokens or 0,
                                 usage.output_tokens or 0, kind="complete")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def complete(system: str, prompt: str, cfg: LLMConfig) -> str:
    """Plain text-only completion — no images, no moments/citation framing.
    For utility tasks that need an LLM call but aren't answering FROM
    retrieved evidence (e.g. DESIGN.md component 17's query enhancement).
    Reuses the same provider dispatch as answer()/caption_image()."""
    if cfg.provider == "anthropic":
        return _complete_anthropic(cfg, system, prompt)
    return _complete_openai(cfg, system, prompt)
