"""Model providers that Token Burn shows as their own column even though a harness (opencode) records them.

opencode labels every request with the provider that served it (its `providerID`). The providers below are
split out of the opencode column into their own platform, so a DeepSeek or NVIDIA model used through
opencode is counted and shown as DeepSeek or NVIDIA, not as opencode. OpenAI is handled the same way (its
adapter also reads Codex), so it keeps its own adapter and is only listed here for the split.

A split platform is "installed" only once it has usage rows in the 30 day window, so a provider the user
never used never gets a column. To add a provider that opencode carries, add one line to HARNESS.
"""

OPENCODE = "opencode"

# platform id -> (display name, opencode provider ids that belong to it). Order is the column order.
HARNESS = {
    "google": ("Google", ("google", "google-vertex", "google-vertex-anthropic", "gemini")),
    "deepseek": ("DeepSeek", ("deepseek",)),
    "nvidia": ("NVIDIA", ("nvidia",)),
    "mistral": ("Mistral", ("mistral",)),
    "qwen": ("Qwen", ("alibaba", "alibaba-cn", "qwen", "dashscope")),
    "meta": ("Meta", ("llama", "meta", "meta-llama")),
    "copilot": ("GitHub Copilot", ("github-copilot", "github-models")),
    "xai": ("xAI Grok", ("xai", "x-ai", "grok")),
}

PLATFORMS = tuple(HARNESS)

_BY_PROVIDER = {pid: plat for plat, (_name, pids) in HARNESS.items() for pid in pids}


def platform_of_provider(provider):
    """The split platform for an opencode provider id, or None when it stays with opencode."""
    if not isinstance(provider, str):
        return None
    return _BY_PROVIDER.get(provider.strip().lower())


def model_prefixes(platform):
    """Model label prefixes ("deepseek/") that rows of this platform carried while still filed under opencode."""
    return tuple(pid + "/" for pid in HARNESS[platform][1])
