"""Reproducibility hashes for `EvalRun`: `prompt_hashes` per role and
`model_config_hash`. See `docs/contracts.md` §7 — "Without `prompt_hashes` a
score is not reproducible and the promotion gate cannot work."
"""

import hashlib
from pathlib import Path

DEFAULT_PROMPTS_DIR = Path("prompts")
DEFAULT_MODELS_CONFIG_PATH = Path("config/models.yaml")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compute_prompt_hashes(prompts_dir: Path = DEFAULT_PROMPTS_DIR) -> dict[str, str]:
    """role -> sha256 of `prompts/<role>.md`. Non-`.md` files (`.gitkeep`) are skipped."""
    return {path.stem: _sha256_file(path) for path in sorted(prompts_dir.glob("*.md"))}


def compute_model_config_hash(
    models_config_path: Path = DEFAULT_MODELS_CONFIG_PATH,
) -> str:
    return _sha256_file(models_config_path)
