const ICONS: Record<string, string> = {
  chatgpt: "openai.svg",
  openai: "openai.svg",
  anthropic: "claude-color.svg",
  nvidia: "nvidia-color.svg",
  groq: "groq-color.svg",
  cerebras: "cerebras-color.svg",
  gemini: "gemini-color.svg",
  mistral: "mistral-color.svg",
  openrouter: "openrouter-color.svg",
  sambanova: "sambanova.svg",
  huggingface: "huggingface-color.svg",
  cloudflare: "workersai-color.svg",
  cohere: "cohere-color.svg",
  "ollama-cloud": "ollama-color.svg",
  ollama: "ollama-color.svg",
  xai: "grok.svg",
  deepseek: "deepseek-color.svg",
  moonshot: "moonshot-color.svg",
  zai: "zai.svg",
  together: "together.svg",
  fireworks: "fireworks-color.svg",
  deepinfra: "deepinfra-color.svg",
  novita: "novita.svg",
  hyperbolic: "hyperbolic-color.svg",
  nebius: "nebius.svg",
  baseten: "baseten.svg",
  "opencode-zen": "opencode.svg",
  "opencode-free": "opencode.svg",
  "opencode-go": "opencode.svg",
  "lm-studio": "lm-studio-color.svg",
  "llama-cpp": "llama-cpp.svg",
  vllm: "vllm-color.svg",
  litellm: "litellm.svg",
}

// White tile so dark monochrome logos stay visible on the dark theme.
export function ProviderIcon({ id, label }: { id: string; label: string }) {
  const file = ICONS[id]
  return file ? (
    <img
      src={`/provider-icons/${file}`}
      alt=""
      aria-hidden
      className="h-6 w-6 shrink-0 rounded bg-white p-0.5"
    />
  ) : (
    <span
      aria-hidden
      className="flex h-6 w-6 shrink-0 items-center justify-center rounded bg-muted text-xs font-semibold"
    >
      {label[0]?.toUpperCase()}
    </span>
  )
}
