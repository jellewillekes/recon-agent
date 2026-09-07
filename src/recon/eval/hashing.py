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
    *additional_config_paths: Path,
) -> str:
    """sha256 of `models_config_path` alone, or - when `additional_config_paths`
    is given (multi mode's `config/roles.yaml`, which also governs a role's
    model/max_turns and so is just as load-bearing for reproducibility) - of
    all paths' bytes concatenated in the order given. Callers must pass a
    stable order for the hash to be reproducible across runs.
    """
    if not additional_config_paths:
        return _sha256_file(models_config_path)
    combined = models_config_path.read_bytes()
    for path in additional_config_paths:
        combined += path.read_bytes()
    return hashlib.sha256(combined).hexdigest()
