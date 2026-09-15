# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=2.54,<3", "pillow>=11,<13"]
# ///
"""Regenerate stock avatars from the saved prompts and painting reference."""

import argparse
import base64
import io
import json
import os
import re
import tempfile
from pathlib import Path

from openai import OpenAI, OpenAIError
from PIL import Image

ROOT = Path(__file__).resolve().parent
MODEL = "gpt-image-2.5-sunburst"


def load_catalog():
    """Read and validate the local prompt catalog before any API calls."""
    catalog = json.loads((ROOT / "prompts.json").read_text(encoding="utf-8"))
    avatars = {}
    for entry in catalog["avatars"]:
        name, prompt = entry["name"], entry["prompt"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
            raise ValueError("Avatar names must be simple lowercase file stems")
        if name in avatars or not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"Duplicate name or empty prompt: {name}")
        avatars[name] = prompt
    reference = ROOT / catalog["reference"]
    if not reference.is_file():
        raise ValueError(f"Painting reference not found: {reference}")
    if not avatars:
        raise ValueError("The catalog contains no avatars")
    return avatars, reference


def api_key():
    """Use a direct API key first, then an optional credential file."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key and (path := os.environ.get("OPENAI_API_KEY_FILE")):
        key = Path(path).read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError("Set OPENAI_API_KEY or OPENAI_API_KEY_FILE")
    return key


def png_bytes(response):
    """Reject missing or invalid image data before touching an output file."""
    if not response.data or not response.data[0].b64_json:
        raise ValueError("OpenAI returned no image")
    data = base64.b64decode(response.data[0].b64_json, validate=True)
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG" or image.width != image.height:
            raise ValueError("OpenAI returned an image that is not a square PNG")
        image.verify()
    return data


def save_png(destination, data, force):
    """Publish only a complete file, preserving existing output on failure."""
    with tempfile.TemporaryDirectory(
        dir=destination.parent, prefix=".avatar-"
    ) as directory:
        temporary = Path(directory) / destination.name
        temporary.write_bytes(data)
        if force:
            temporary.replace(destination)
        else:
            # A hard link publishes the complete file without replacing a concurrent output.
            os.link(temporary, destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help="Avatar names; omit to generate all")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "generated",
        help="Destination directory (default: generated/ beside this script)",
    )
    parser.add_argument(
        "--model", default=MODEL, help=f"Image model (default: {MODEL})"
    )
    parser.add_argument(
        "--force", action="store_true", help="Replace existing output files"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="List work without API calls or writes"
    )
    args = parser.parse_args(argv)

    try:
        avatars, reference = load_catalog()
        names = list(dict.fromkeys(args.names)) or list(avatars)
        unknown = set(names) - avatars.keys()
        if unknown:
            parser.error(
                f"Unknown avatars: {', '.join(sorted(unknown))}. Available: {', '.join(avatars)}"
            )

        pending = []
        for name in names:
            destination = args.output_dir / f"{name}.png"
            if destination.exists() and not args.force:
                print(f"Skip {name}: {destination} already exists", flush=True)
            else:
                pending.append((name, destination))

        if args.dry_run:
            for name, destination in pending:
                print(f"Would generate {name} with {args.model}: {destination}")
            return
        if not pending:
            return

        with OpenAI(api_key=api_key(), timeout=600.0) as client:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            if not args.force:
                with tempfile.TemporaryDirectory(
                    dir=args.output_dir, prefix=".avatar-"
                ) as directory:
                    probe = Path(directory) / "probe"
                    probe.touch()
                    try:
                        os.link(probe, probe.with_suffix(".link"))
                    except OSError as error:
                        raise ValueError(
                            "Output directory does not permit hard links; choose another directory "
                            "or use --force (replaces existing output)"
                        ) from error
            for name, destination in pending:
                print(f"Generating {name} with {args.model}...", flush=True)
                with reference.open("rb") as image:
                    response = client.images.edit(
                        model=args.model,
                        image=image,
                        prompt=avatars[name],
                        size="1024x1024",
                        quality="high",
                        output_format="png",
                    )
                data = png_bytes(response)
                save_png(destination, data, args.force)
                print(f"Saved {destination}", flush=True)
    except (OSError, ValueError, KeyError, TypeError, OpenAIError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    main()
