"""TRL DPO + LoRA trainer. Heavy dependencies are imported lazily so
the rest of the package stays installable without torch et al."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .self_play import PreferencePair
from .spec import PersonaSpec


@dataclass
class TrainedAdapter:
    persona_name: str
    base_model: str
    output_dir: Path
    pushed_to_hub: Optional[str] = None


def train(spec: PersonaSpec, pairs: List[PreferencePair], output_dir: Path) -> TrainedAdapter:
    """Train a LoRA adapter via TRL's DPOTrainer. Imports torch /
    transformers / trl / peft lazily; raises if they are not
    installed. The caller chooses where to write the artifacts."""
    if not spec.is_trainable or spec.training is None:
        raise ValueError(f"persona {spec.name!r} is not configured for training")

    try:
        import torch  # noqa: F401
        from datasets import Dataset
        from peft import LoraConfig, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from trl import DPOConfig, DPOTrainer
    except ImportError as e:
        raise RuntimeError(
            "training requires the `torch` extra: `uv add 'ensemble-train[torch]'`"
        ) from e

    output_dir.mkdir(parents=True, exist_ok=True)
    base = spec.training.base_model
    tokenizer = AutoTokenizer.from_pretrained(base)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(base)
    lora_cfg = LoraConfig(
        r=int(spec.training.lora.get("r", 16)),
        lora_alpha=int(spec.training.lora.get("alpha", 32)),
        lora_dropout=float(spec.training.lora.get("dropout", 0.05)),
        target_modules=list(spec.training.lora.get("target_modules", ["q_proj", "v_proj"])),
    )
    model = get_peft_model(model, lora_cfg)

    dataset = Dataset.from_list(
        [
            {"prompt": p.prompt, "chosen": p.chosen, "rejected": p.rejected}
            for p in pairs
        ]
    )

    dpo_cfg = DPOConfig(
        output_dir=str(output_dir),
        beta=float(spec.training.dpo.get("beta", 0.1)),
        per_device_train_batch_size=int(spec.training.dpo.get("batch_size", 4)),
        num_train_epochs=int(spec.training.dpo.get("epochs", 1)),
        learning_rate=float(spec.training.dpo.get("learning_rate", 5e-6)),
        max_length=int(spec.training.dpo.get("max_length", 2048)),
    )
    trainer = DPOTrainer(
        model=model,
        args=dpo_cfg,
        tokenizer=tokenizer,
        train_dataset=dataset,
    )
    trainer.train()
    trainer.save_model(str(output_dir))

    pushed: Optional[str] = None
    namespace_env = spec.training.hf_namespace_env
    hf_user = os.environ.get(namespace_env, "").strip()
    if hf_user and spec.training.adapter_name:
        repo_id = f"{hf_user}/{spec.training.adapter_name}"
        try:
            model.push_to_hub(repo_id)
            pushed = repo_id
        except Exception as e:  # don't fail the run on a push error
            print(f"warn: hub push failed: {e}")

    # Emit a persona card so reviewers can see what this run produced.
    (output_dir / "persona-card.md").write_text(
        _render_card(spec, pairs, pushed),
        encoding="utf-8",
    )

    return TrainedAdapter(
        persona_name=spec.name,
        base_model=base,
        output_dir=output_dir,
        pushed_to_hub=pushed,
    )


def _render_card(spec: PersonaSpec, pairs: List[PreferencePair], pushed: Optional[str]) -> str:
    parts: List[str] = []
    parts.append(f"# {spec.name}\n")
    parts.append(f"{spec.description}\n")
    parts.append(f"- base model: `{spec.training.base_model if spec.training else 'n/a'}`")
    parts.append(f"- mode: {spec.mode}")
    parts.append(f"- preference pairs: {len(pairs)}")
    if pushed:
        parts.append(f"- pushed adapter: `{pushed}`")
    parts.append("")
    parts.append("## Style")
    parts.append("```toml")
    parts.append(json.dumps(spec.style, indent=2))
    parts.append("```")
    parts.append("")
    parts.append("## Sample pair")
    if pairs:
        p = pairs[0]
        parts.append(f"_Prompt:_ {p.prompt}")
        parts.append(f"_Chosen:_ {p.chosen}")
        parts.append(f"_Rejected:_ {p.rejected[:120]}...")
    return "\n".join(parts) + "\n"
