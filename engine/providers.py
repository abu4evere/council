"""Provider registry.

Every provider here speaks the OpenAI-compatible chat-completions protocol, so
one client handles all of them -- only the base URL and the API key change.

FREE TIERS. Each of these has a free tier that needs no card. Limits and model
names change often, so treat the numbers below as "roughly what to expect" and
confirm at the signup link. `python check_key.py` validates whatever you have
configured against the live APIs.

You do NOT need all of them. Council runs with whatever keys are present and
skips the rest -- one key works, four is better because the answers differ more.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    base_url: str
    env_var: str | None      # None = no auth needed (local Ollama)
    signup: str
    notes: str
    # How many seats may call this provider at once. This is a property of the
    # provider's rate limit, not a global setting -- Groq's 8000 tokens/minute
    # forces seats to queue, while Gemini's ceiling is high enough to fan out.
    max_parallel: int = 2
    # Distinct CHAT models known to exist on this provider, best first. Used to
    # keep the council varied when few providers are configured: with only one
    # key, seats are spread across that provider's different models rather than
    # all collapsing onto one. Verified entries only -- check_key.py validates.
    alternates: tuple[str, ...] = ()
    # Hard ceiling on max_tokens for THIS provider. Budgets have to be per
    # provider, not global: Groq's 8000 tokens/minute forces small requests,
    # while Gemini and Cerebras have their own, far larger buckets. Sizing
    # everything to the tightest provider starved the reasoning models on the
    # others, which then returned nothing at all.
    token_cap: int = 8000

    def url(self) -> str:
        """base_url with any ${ENV_VAR} placeholders filled in.

        Cloudflare puts the account id in the path rather than a header, so the
        URL itself has to be templated.
        """
        out = self.base_url
        while "${" in out:
            start = out.index("${")
            end = out.index("}", start)
            var = out[start + 2:end]
            out = out[:start] + os.environ.get(var, "").strip() + out[end + 1:]
        return out

    def api_key(self) -> str | None:
        """The key to use right now.

        The signed-in user's own key wins over the server's. Reading
        os.environ alone was fine for one person on a laptop and wrong the
        moment two people run at once -- the environment is process-global,
        so whoever started last would have been spending the other's quota.
        """
        if self.env_var is None:
            return None
        from .keyring import current_key
        mine = current_key(self.env_var)
        if mine:
            return mine
        if os.environ.get("BYOK_ONLY", "").strip().lower() in ("1", "true", "yes"):
            # Hosted mode: never fall back to the operator's keys.
            return None
        return os.environ.get(self.env_var, "").strip() or None

    def configured(self) -> bool:
        """Ollama is 'configured' if reachable; we check that at call time."""
        return self.env_var is None or bool(self.api_key())


PROVIDERS: dict[str, Provider] = {
    "groq": Provider(
        key="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        env_var="GROQ_API_KEY",
        signup="https://console.groq.com/keys",
        notes="Free, no card. Very fast. 8000 tokens/MINUTE is the binding limit.",
        max_parallel=1,
        # Verified live 2026-09-27. The rest of Groq's catalogue is speech and
        # safety classifiers, not chat models.
        alternates=("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"),
        # 8000 TPM total, and seats here run one at a time, so a single request
        # can use most of the window -- but not all of it, since the input
        # counts too and a 413 is unrecoverable.
        token_cap=3500,
    ),
    "gemini": Provider(
        key="gemini",
        label="Google AI Studio",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        env_var="GEMINI_API_KEY",
        signup="https://aistudio.google.com/apikey",
        notes="Free, no card. Much higher token ceiling than Groq, and a big context "
              "window -- the right home for the seats with large inputs.",
        max_parallel=3,
        # Verified live 2026-09-27. The 2.x line is retired for new accounts,
        # and the pro models 429 immediately on the free quota.
        # gemma-* are Google's OPEN models: a different family from gemini,
        # trained differently, and they disagree with it more often than
        # another gemini version would. Verified live 2026-09-27. They emit
        # <thought> tags inline, which engine/thinking.py strips.
        alternates=("gemini-3.8-flash", "gemma-4-31b-it", "gemini-3.5-flash",
                    "gemini-flash-latest", "gemma-4-26b-a4b-it",
                    "gemini-3-flash-preview", "gemini-3.1-flash-lite"),
        token_cap=8000,
    ),
    "cerebras": Provider(
        key="cerebras",
        label="Cerebras",
        base_url="https://api.cerebras.ai/v1",
        env_var="CEREBRAS_API_KEY",
        signup="https://cloud.cerebras.ai",
        notes="Free tier, no card. Very fast. NOTE: its catalogue duplicates Groq's "
              "(gpt-oss-120b, qwen), so it buys a separate rate-limit bucket rather "
              "than a genuinely different voice.",
        max_parallel=2,
        alternates=("gpt-oss-120b", "qwen-3.8-27b"),  # verified live 2026-09-27
        # Both of its models are reasoning models that burn tokens thinking
        # before they write anything. Starve them and they return empty.
        token_cap=8000,
    ),
    "github": Provider(
        key="github",
        label="GitHub Models",
        base_url="https://models.github.ai/inference",
        env_var="GITHUB_TOKEN",
        signup="https://github.com/settings/tokens - fine-grained token, and you MUST "
               "grant the 'Models' permission (read). Without it the endpoint returns "
               "a plain-text 200 'OK' instead of a completion.",
        notes="Free with any GitHub account. Hosts several vendors' models.",
        alternates=("openai/gpt-4o-mini",),
    ),
    "mistral": Provider(
        key="mistral",
        label="Mistral",
        base_url="https://api.mistral.ai/v1",
        env_var="MISTRAL_API_KEY",
        signup="https://console.mistral.ai",
        notes="Free experiment tier. Needs phone verification.",
    ),
    "openrouter": Provider(
        key="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        env_var="OPENROUTER_API_KEY",
        signup="https://openrouter.ai/keys",
        notes="Use ':free' model slugs to pay nothing. Low daily cap without credit.",
    ),
    "sambanova": Provider(
        key="sambanova",
        label="SambaNova",
        base_url="https://api.sambanova.ai/v1",
        env_var="SAMBANOVA_API_KEY",
        signup="https://cloud.sambanova.ai/apis",
        notes="Free tier, no card. Serves LLAMA models -- a family you do not "
              "otherwise have.",
        max_parallel=2,
        alternates=(),   # run check_key.py to discover; slugs unverified
    ),
    "nvidia": Provider(
        key="nvidia",
        label="NVIDIA NIM",
        base_url="https://integrate.api.nvidia.com/v1",
        env_var="NVIDIA_API_KEY",
        signup="https://build.nvidia.com (free credits, no card)",
        notes="Hosts Llama, DeepSeek, Nemotron and others. Widest family variety "
              "of the free options.",
        max_parallel=2,
        # Verified live 2026-09-27. NOTE: its /models listing includes entries
        # it will not actually serve -- mistral-large-2-instruct and
        # llama-3.1-nemotron-70b-instruct are listed but 404 on use. Only
        # models confirmed by a real call belong here.
        alternates=("deepseek-ai/deepseek-v4.1-flash", "moonshotai/kimi-k3",
                    "nvidia/nemotron-3-super-120b-a12b"),
        token_cap=8000,
    ),
    "huggingface": Provider(
        key="huggingface",
        label="Hugging Face",
        base_url="https://router.huggingface.co/v1",
        env_var="HF_TOKEN",
        signup="https://huggingface.co/settings/tokens (read token is enough)",
        notes="Router in front of many open models. Free tier is small but the "
              "variety is unmatched.",
        max_parallel=1,
        alternates=(),
    ),
    "cloudflare": Provider(
        key="cloudflare",
        label="Cloudflare Workers AI",
        base_url="https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/ai/v1",
        env_var="CF_API_TOKEN",
        signup="https://dash.cloudflare.com -> AI -> Workers AI. Needs BOTH "
               "CF_API_TOKEN and CF_ACCOUNT_ID.",
        notes="Free daily allowance. Serves Llama, Mistral, Gemma, Qwen.",
        max_parallel=2,
        alternates=(),
    ),
    "ollama": Provider(
        key="ollama",
        label="Ollama (local)",
        base_url="http://localhost:11434/v1",
        env_var=None,
        signup="https://ollama.com/download",
        notes="Unlimited and private, but needs real RAM/GPU. Not viable under ~16GB.",
    ),
}


def get(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise KeyError(
            f"unknown provider {name!r}. Known: {', '.join(sorted(PROVIDERS))}"
        ) from None


def configured_providers() -> list[Provider]:
    return [p for p in PROVIDERS.values() if p.env_var and p.configured()]
