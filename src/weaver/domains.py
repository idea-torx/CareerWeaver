"""Skill taxonomy: the nine domains, keyword classification, tool vocabulary.

Everything here is deterministic — no LLM. The classifier is what lets a lens
re-order the same fact graph into a different narrative.
"""

from __future__ import annotations

import re

# name, display label, keywords (multi-word keywords score higher)
DOMAINS: list[tuple[str, str, list[str]]] = [
    (
        "cgi_motion",
        "CGI & Motion",
        [
            "cgi", "blender", "3d", "render", "rendering", "photoreal", "modeling",
            "modelling", "uv mapping", "rigging", "lighting", "compositing",
            "animation", "animated", "motion design", "motion graphics", "scene design",
            "product visualization", "product renders", "product imagery", "visual effects",
            "hero animation", "drone animation", "concept boards", "concept visualization",
        ],
    ),
    (
        "design_engineering",
        "Design Engineering",
        [
            "design engineer", "ui", "ux", "ui/ux", "interface", "figma", "prototype",
            "prototyping", "design system", "design systems", "interaction design",
            "product design", "wireframe", "wireframes", "mockup", "mockups",
            "typography", "front-end", "frontend", "infinite canvas", "node canvas",
            "human-ai interaction", "human-machine interface", "ux for agentic systems",
            "product surface", "product surfaces", "user experience",
        ],
    ),
    (
        "video_multimedia",
        "Video & Multimedia",
        [
            "video", "storyboard", "storyboards", "storyboarding", "shot design",
            "shot list", "editing", "video editing", "final edit", "sound", "audio design",
            "audio", "reel", "reels", "cutdown", "cutdowns", "commercial", "super bowl",
            "teaser", "teasers", "multimedia", "post-production", "film", "filmmaking",
            "cinema", "campaign content", "social cutdowns", "promo video", "footage",
            "pitch video", "intro video", "content extensions",
        ],
    ),
    (
        "graphics_brand",
        "Graphics & Brand",
        [
            "brand", "branding", "brand identity", "rebrand", "visual identity", "logo",
            "packaging", "marketing site", "marketing website", "marketing assets",
            "email marketing", "pitch deck", "pitch decks", "graphic", "graphics",
            "collateral", "merchandise", "booklets", "campaign", "positioning",
            "brand refresh", "visual language", "color system", "one-pager", "ads",
        ],
    ),
    (
        "direction_pm",
        "Direction & Delivery",
        [
            "creative direction", "creative director", "art direction", "directed",
            "direction", "roadmap", "leadership", "led the", "team", "vendor",
            "project management", "producer", "producers", "cross-functional",
            "gtm", "icp", "strategy", "budget", "stakeholder", "hiring", "sdlc",
            "client delivery", "partner management", "content strategy", "team leadership",
            "managed", "owned the roadmap", "accelerator", "positioning",
        ],
    ),
    (
        "ai_expertise",
        "AI & Machine Learning",
        [
            "ai", "generative ai", "genai", "machine learning", "stable diffusion",
            "diffusion", "flux", "lora", "fine-tuning", "fine-tune", "fine-tuned",
            "synthetic data", "prompt engineering", "model fidelity", "model evaluation",
            "hyperparameter", "rag", "fal.ai", "kling", "seedance", "nano banana",
            "gpt-image", "frontier llms", "llm", "inference", "training", "ai-native",
            "amazon rekognition", "computer vision", "ai concept visualization",
        ],
    ),
    (
        "sre_cloud",
        "Cloud & Reliability",
        [
            "site reliability", "sre", "infrastructure", "deployment", "deployed",
            "staging", "production environment", "staging-to-production", "cloudflare",
            "cloudflare r2", "r2 storage", "gpu infrastructure", "gpu", "uptime",
            "scaling", "at scale", "ci/cd", "monitoring", "observability", "docker",
            "kubernetes", "aws", "reliability", "race conditions", "job pipeline",
            "async job pipeline", "polling", "compliance", "app store compliance",
        ],
    ),
    (
        "agentic_engineering",
        "Agentic Engineering",
        [
            "agent", "agents", "agentic", "multi-agent", "multi-agent systems",
            "agentic workflows", "ai coding agents", "orchestrate", "orchestrated",
            "orchestrating", "orchestration", "claude code", "codex", "lovable",
            "replit", "mcp", "tool use", "workflow automation", "agentic filmmaking",
            "guardrails", "policy guardrails",
        ],
    ),
    (
        "fullstack_engineering",
        "Full-Stack Engineering",
        [
            "typescript", "javascript", "react", "node.js", "node", "python",
            "full-stack", "fullstack", "backend", "api", "api design", "orm",
            "database", "postgres", "sql", "redis", "convex", "auth", "workos",
            "clerk", "capacitor", "ios", "mobile", "real-time", "multiplayer",
            "sync", "billing", "webhooks", "architected", "shipped", "built",
            "custom typescript orm", "data layer", "schema",
        ],
    ),
]

DOMAIN_NAMES: list[str] = [d[0] for d in DOMAINS]

# Common LLM/industry variants → canonical display label. Keeps generated
# resumes consistent and the guardrail quiet (unknown "orgs" are usually
# invented domain labels like "SRE & Cloud").
_DOMAIN_ALIASES: dict[str, str] = {
    "sre": "Cloud & Reliability",
    "sre & cloud": "Cloud & Reliability",
    "cloud": "Cloud & Reliability",
    "reliability": "Cloud & Reliability",
    "pm": "Direction & Delivery",
    "project management": "Direction & Delivery",
    "direction & pm": "Direction & Delivery",
    "delivery": "Direction & Delivery",
    "creative direction": "Direction & Delivery",
    "cgi": "CGI & Motion",
    "motion": "CGI & Motion",
    "multimedia": "Video & Multimedia",
    "video": "Video & Multimedia",
    "design engineering": "Design Engineering",
    "graphics": "Graphics & Brand",
    "brand": "Graphics & Brand",
    "ai": "AI & Machine Learning",
    "machine learning": "AI & Machine Learning",
    "agentic": "Agentic Engineering",
    "full stack": "Full-Stack Engineering",
    "full-stack": "Full-Stack Engineering",
}


def canonical_domain(label: str) -> str:
    """Map a free-form/LM domain label onto the canonical display label."""
    text = (label or "").strip().lower().replace("_", " ")
    if not text:
        return label or "Skills"
    if text in _DOMAIN_ALIASES:
        return _DOMAIN_ALIASES[text]
    for name, display in DOMAIN_LABELS.items():
        if text == display.lower() or text == name.replace("_", " "):
            return display
        # Keyword-overlap fallback: every significant word of the canonical
        # label appears in the candidate text.
        sig = {w for w in display.lower().replace("&", "").split() if len(w) > 2}
        if sig and sig.issubset(set(re.findall(r"[a-z]+", text))):
            return display
    return label
DOMAIN_LABELS: dict[str, str] = {d[0]: d[1] for d in DOMAINS}
DOMAIN_KEYWORDS: dict[str, list[str]] = {d[0]: d[2] for d in DOMAINS}

# Section-heading hints from the seed resumes -> domain, used when classifying
# a whole SKILLS category line ("CGI & Motion:  Blender, ...").
CATEGORY_HINTS: dict[str, str] = {
    "ai & machine learning": "ai_expertise",
    "generative ai": "ai_expertise",
    "ai dev tooling": "agentic_engineering",
    "agentic / forward-deployed engineering": "agentic_engineering",
    "engineering": "fullstack_engineering",
    "cgi & motion": "cgi_motion",
    "cgi & motion design": "cgi_motion",
    "video & multimedia": "video_multimedia",
    "web & brand": "graphics_brand",
    "brand & design": "graphics_brand",
    "design & brand": "graphics_brand",
    "human-machine interface": "design_engineering",
    "direction": "direction_pm",
    "creative leadership": "direction_pm",
    "creative & strategic leadership": "direction_pm",
    "delivery & leadership": "direction_pm",
}

# Recognised tools -> canonical spelling. Drives `tool` facts.
TOOLS: dict[str, str] = {
    "blender": "Blender",
    "figma": "Figma",
    "typescript": "TypeScript",
    "javascript": "JavaScript",
    "react": "React",
    "node.js": "Node.js",
    "python": "Python",
    "postgres": "Postgres",
    "redis": "Redis",
    "convex": "Convex",
    "clerk": "Clerk",
    "workos": "WorkOS",
    "capacitor": "Capacitor",
    "cloudflare r2": "Cloudflare R2",
    "stable diffusion": "Stable Diffusion",
    "flux": "Flux",
    "fal.ai": "fal.ai",
    "kling": "Kling",
    "seedance": "Seedance",
    "nano banana": "Nano Banana",
    "gpt-image": "GPT-Image",
    "claude code": "Claude Code",
    "codex": "Codex",
    "lovable": "Lovable",
    "replit": "Replit",
    "git": "Git",
    "github": "GitHub",
    "amazon rekognition": "Amazon Rekognition",
    "after effects": "After Effects",
    "premiere": "Premiere",
}

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^a-z0-9+#./&\- ]+")
_SMART = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "·": " "})


def normalize(text: str) -> str:
    """Lowercase, de-smart-quote, strip punctuation, collapse whitespace."""
    if not text:
        return ""
    t = text.translate(_SMART).lower()
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


def _hits(haystack: str, keyword: str) -> int:
    """Count keyword occurrences on rough word boundaries."""
    kw = normalize(keyword)
    if not kw:
        return 0
    pattern = r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])"
    return len(re.findall(pattern, haystack))


def classify(text: str, max_domains: int = 4, min_score: int = 1) -> list[str]:
    """Return domain names for a chunk of text, strongest first."""
    scores = score_domains(text)
    ranked = [name for name, s in scores.items() if s >= min_score]
    ranked.sort(key=lambda n: (-scores[n], DOMAIN_NAMES.index(n)))
    return ranked[:max_domains]


def score_domains(text: str) -> dict[str, int]:
    """Raw domain scores for a chunk of text (multi-word keywords weigh 2)."""
    hay = normalize(text)
    out: dict[str, int] = {}
    if not hay:
        return {name: 0 for name in DOMAIN_NAMES}
    for name, _label, keywords in DOMAINS:
        total = 0
        for kw in keywords:
            n = _hits(hay, kw)
            if n:
                total += n * (2 if " " in kw.strip() else 1)
        out[name] = total
    return out


def label(domain: str) -> str:
    return DOMAIN_LABELS.get(domain, domain.replace("_", " ").title())


def find_tools(text: str) -> list[str]:
    """Canonical tool names mentioned in text."""
    hay = normalize(text)
    found: list[str] = []
    for key, canonical in TOOLS.items():
        if _hits(hay, key):
            found.append(canonical)
    return found
