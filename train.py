import os

os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"

import sys

sys.path.append("ai-toolkit")
sys.path.append("LLaVA")

from submodule_patches import patch_submodules

patch_submodules()

import sys
import torch
from typing import OrderedDict, Optional
import shutil
import subprocess
from zipfile import ZipFile
from cog import BaseModel, Input, Path, Secret
from huggingface_hub import HfApi

from jobs import ExtensionJob
from toolkit.config import get_config
from caption import Captioner

WEIGHTS_PATH = Path("./FLUX.1-dev")
INPUT_DIR = Path("input_images")
OUTPUT_DIR = Path("output")


class TrainingOutput(BaseModel):
    weights: Path


def train(
    input_images: Path = Input(
        description="A zip file containing the images that will be used for training. We recommend a minimum of 10 images. If you include captions, include them as one .txt file per image, e.g. my-photo.jpg should have a caption file named my-photo.txt. If you don't include captions, you can use autocaptioning (enabled by default)."
    ),
    trigger_word: str = Input(
        description="The trigger word refers to the object, style or concept you are training on. Pick a string that isn’t a real word, like TOK or something related to what’s being trained, like CYBRPNK. The trigger word you specify here will be associated with all images during training. Then when you use your LoRA, you can include the trigger word in prompts to help activate the LoRA.",
        default="TOK",
    ),
    autocaption: bool = Input(
        description="Automatically caption images using Llava v1.5 13B", default=True
    ),
    autocaption_prefix: str = Input(
        description="Optional: Text you want to appear at the beginning of all your generated captions; for example, ‘a photo of TOK, ’. You can include your trigger word in the prefix. Prefixes help set the right context for your captions, and the captioner will use this prefix as context.",
        default=None,
    ),
    autocaption_suffix: str = Input(
        description="Optional: Text you want to appear at the end of all your generated captions; for example, ‘ in the style of TOK’. You can include your trigger word in suffixes. Suffixes help set the right concept for your captions, and the captioner will use this suffix as context.",
        default=None,
    ),
    steps: int = Input(
        description="Number of training steps. Recommended range 500-4000",
        ge=10,
        le=4000,
        default=1000,
    ),
    learning_rate: float = Input(
        description="Learning rate, if you’re new to training you probably don’t need to change this.",
        default=4e-4,
    ),
    batch_size: int = Input(
        description="Batch size, you can leave this as 1", default=1
    ),
    hf_repo_id: str = Input(
        description="Hugging Face repository ID, if you'd like to upload the trained LoRA to Hugging Face. For example, lucataco/flux-dev-lora.",
        default=None,
    ),
    hf_token: Secret = Input(
        description="Hugging Face token, if you'd like to upload the trained LoRA to Hugging Face.",
        default=None,
    ),
) -> TrainingOutput:
    clean_up()
    output_path = "/tmp/trained_model.tar"

    # Shortcut training and host a pretrained LoRA from Hugging Face
    # This has the advantage of putting the LoRA onto the hotswap base
    if (
        input_images.startswith("https://huggingface.co")
        and ".safetensors" in input_images
    ):
        download_huggingface_lora(input_images)
        os.system(f"tar -cvf {output_path} /tmp/flux_train_replicate")
        return TrainingOutput(weights=Path(output_path))

    download_weights()
    extract_zip(input_images, INPUT_DIR)

    train_config = OrderedDict(
        {
            "job": "extension",
            "config": {
                "name": "flux_train_replicate",
                "process": [
                    {
                        "type": "sd_trainer",
                        "training_folder": str(OUTPUT_DIR),
                        "device": "cuda:0",
                        "trigger_word": trigger_word,
                        "network": {"type": "lora", "linear": 16, "linear_alpha": 16},
                        "save": {
                            "dtype": "float16",
                            "save_every": steps + 1,
                            "max_step_saves_to_keep": 1,
                        },
                        "datasets": [
                            {
                                "folder_path": str(INPUT_DIR),
                                "caption_ext": "filename",
                                "caption_dropout_rate": 0.05,
                                "shuffle_tokens": False,
                                "cache_latents_to_disk": True,
                                "resolution": [512, 768, 1024],
                            }
                        ],
                        "train": {
                            "batch_size": batch_size,
                            "steps": steps,
                            "gradient_accumulation_steps": 1,
                            "train_unet": True,
                            "train_text_encoder": False,
                            "content_or_style": "balanced",
                            "gradient_checkpointing": True,
                            "noise_scheduler": "flowmatch",
                            "optimizer": "adamw8bit",
                            "lr": learning_rate,
                            "ema_config": {"use_ema": True, "ema_decay": 0.99},
                            "dtype": "bf16",
                        },
                        "model": {
                            "name_or_path": str(WEIGHTS_PATH),
                            "is_flux": True,
                            "quantize": True,
                        },
                        "sample": {
                            "sampler": "flowmatch",
                            "sample_every": steps + 1,
                            "width": 1024,
                            "height": 1024,
                            "prompts": [],
                            "neg": "",
                            "seed": 42,
                            "walk_seed": True,
                            "guidance_scale": 4,
                            "sample_steps": 20,
                        },
                    }
                ],
            },
            "meta": {"name": "[name]", "version": "1.0"},
        }
    )

    if not trigger_word:
        del train_config["config"]["process"][0]["trigger_word"]

    captioner = Captioner()
    if autocaption and not captioner.all_images_are_captioned(INPUT_DIR):
        captioner.load_models()
        captioner.caption_images(INPUT_DIR, autocaption_prefix, autocaption_suffix)

    del captioner
    torch.cuda.empty_cache()

    print("Starting train job")
    job = ExtensionJob(get_config(train_config, name=None))
    job.run()
    job.cleanup()

    lora_dir = OUTPUT_DIR / "flux_train_replicate"
    lora_file = lora_dir / "flux_train_replicate.safetensors"
    lora_file.rename(lora_dir / "lora.safetensors")

    # Optimizer is used to continue training, not needed in output
    optimizer_file = lora_dir / "optimizer.pt"
    if optimizer_file.exists():
        os.remove(optimizer_file)

    os.system(f"tar -cvf {output_path} {lora_dir}")

    if hf_token is not None and hf_repo_id is not None:
        try:
            handle_hf_readme(lora_dir, hf_repo_id, trigger_word)
            print(f"Uploading to Hugging Face: {hf_repo_id}")
            api = HfApi()
            api.upload_folder(
                repo_id=hf_repo_id,
                folder_path=lora_dir,
                repo_type="model",
                use_auth_token=hf_token.get_secret_value(),
            )
        except Exception as e:
            print(f"Error uploading to Hugging Face: {str(e)}")

    return TrainingOutput(weights=Path(output_path))


def handle_hf_readme(lora_dir: Path, hf_repo_id: str, trigger_word: Optional[str]):
    os.system(f"cp lora-license.md {lora_dir}/README.md")

    with open(f"{lora_dir}/README.md", "r") as f:
        content = f.read()

    content = content.replace("[hf_repo_id]", hf_repo_id)

    repo_parts = hf_repo_id.split("/")
    if len(repo_parts) > 1:
        title = repo_parts[1].replace("-", " ").title()
        content = content.replace("[title]", title)
    else:
        content = content.replace("[title]", hf_repo_id)

    if trigger_word:
        content = content.replace(
            "[trigger_section]",
            f"\n## Trigger words\nYou should use `{trigger_word}` to trigger the image generation.\n",
        )
        content = content.replace(
            "[instance_prompt]", f"instance_prompt: {trigger_word}"
        )
    else:
        content = content.replace("[trigger_section]", "")
        content = content.replace("[instance_prompt]", "")

    print(content)

    with open(f"{lora_dir}/README.md", "w") as f:
        f.write(content)


def extract_zip(input_images: Path, input_dir: Path):
    if not input_images.name.endswith(".zip"):
        raise ValueError("input_images must be a zip file")

    input_dir.mkdir(parents=True, exist_ok=True)
    image_count = 0
    with ZipFile(input_images, "r") as zip_ref:
        for file_info in zip_ref.infolist():
            if not file_info.filename.startswith(
                "__MACOSX/"
            ) and not file_info.filename.startswith("._"):
                zip_ref.extract(file_info, input_dir)
                image_count += 1

    print(f"Extracted {image_count} files from zip to {input_dir}")


def clean_up():
    if INPUT_DIR.exists():
        shutil.rmtree(INPUT_DIR)

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)


def download_huggingface_lora(hf_lora_url: str):
    lora_path = "/tmp/flux_train_replicate/lora.safetensors"
    if os.path.exists(lora_path):
        os.remove(lora_path)

    print(f"Downloading {hf_lora_url} to {lora_path}")
    subprocess.check_output(
        [
            "pget",
            "-f",
            hf_lora_url,
            lora_path,
        ]
    )


def download_weights():
    if not WEIGHTS_PATH.exists():
        subprocess.check_output(
            [
                "pget",
                "-xf",
                "https://weights.replicate.delivery/default/black-forest-labs/FLUX.1-dev/files.tar",
                str(WEIGHTS_PATH.parent),
            ]
        )
