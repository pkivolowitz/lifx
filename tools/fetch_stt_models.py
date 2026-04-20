#!/usr/bin/env python3
"""Pre-fetch Whisper STT models for the voice coordinator.

Downloads the MLX-Whisper and faster-whisper model weights to a local
directory (default ``/Volumes/Mini-Dock/glowup/models/`` on Daedalus)
so the coordinator can start without a cold HF download, and so the
model storage survives a ``~/.cache/huggingface/`` wipe.

Design goals:
    - Idempotent.  snapshot_download skips files already present with
      matching hashes, so re-running is cheap.
    - Two-copy storage.  snapshot_download populates both the HF cache
      and the local_dir on every call, so models remain recoverable
      from either location if the other is lost — no single point of
      failure for the on-disk weights.
    - No hidden state.  Paths are constructed explicitly and printed
      before each download so the operator can see where bytes land.

Repo sources (verified against faster_whisper.utils._MODELS on
2026-04-20):
    mlx-whisper turbo    → mlx-community/whisper-large-v3-turbo
    faster-whisper turbo → mobiuslabsgmbh/faster-whisper-large-v3-turbo

Usage::

    tools/fetch_stt_models.py                        # default: both engines, turbo
    tools/fetch_stt_models.py --engine mlx-whisper   # one engine only
    tools/fetch_stt_models.py --model large-v3       # different model
    tools/fetch_stt_models.py --root /some/path      # different model root

Run on Daedalus after deploy, before restarting the coordinator.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import argparse
import logging
import os
import sys
import time
from typing import Optional

logger: logging.Logger = logging.getLogger("glowup.fetch_stt_models")

DEFAULT_MODEL_ROOT: str = "/Volumes/Mini-Dock/glowup/models"
DEFAULT_MODEL: str = "large-v3-turbo"

# Engine → HF repo ID for the given model name.  Keep in lock-step
# with voice/coordinator/stt_engines/.  If a new engine is added, add
# its registry here.
_REPO_TEMPLATES: dict[str, str] = {
    "mlx-whisper": "mlx-community/whisper-{model}",
    "faster-whisper": "mobiuslabsgmbh/faster-whisper-{model}",
}


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _target_dir(root: str, engine: str, model: str) -> str:
    """Return the local directory layout the facade expects.

    Must stay in sync with ``_resolve_model_path`` in
    ``voice/coordinator/stt.py``: ``<root>/<engine>/<model>/``.
    """
    return os.path.join(root, engine, model)


def fetch(
    engine: str,
    model: str,
    root: str,
    force: bool,
) -> str:
    """Download weights for one (engine, model) pair.

    Returns the local directory path where files ended up.  Raises on
    download failure so the exit code reflects the problem.
    """
    template: Optional[str] = _REPO_TEMPLATES.get(engine)
    if template is None:
        raise ValueError(
            f"Unknown engine '{engine}' — valid: {sorted(_REPO_TEMPLATES)}"
        )
    repo_id: str = template.format(model=model)
    local_dir: str = _target_dir(root, engine, model)

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "huggingface_hub not installed — activate the coordinator "
            "venv (e.g. ~/venv/bin/python) and re-run."
        ) from exc

    if os.path.isdir(local_dir) and not force and os.listdir(local_dir):
        logger.info(
            "%s: %s already present at %s (use --force to re-download)",
            engine, model, local_dir,
        )
        return local_dir

    os.makedirs(local_dir, exist_ok=True)
    logger.info("%s: downloading %s → %s", engine, repo_id, local_dir)
    t0: float = time.monotonic()
    snapshot_download(
        repo_id=repo_id,
        local_dir=local_dir,
        local_dir_use_symlinks=False,
    )
    elapsed: float = time.monotonic() - t0
    logger.info(
        "%s: done in %.1fs — files at %s (and HF cache)",
        engine, elapsed, local_dir,
    )
    return local_dir


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None,
    )
    parser.add_argument(
        "--engine",
        action="append",
        choices=sorted(_REPO_TEMPLATES),
        help="Engine to fetch (repeatable). Default: both.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model name (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--root",
        default=DEFAULT_MODEL_ROOT,
        help=f"Model root directory (default: {DEFAULT_MODEL_ROOT}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if the local directory already exists.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    engines: list[str] = args.engine or sorted(_REPO_TEMPLATES)

    if not os.path.isdir(args.root):
        try:
            os.makedirs(args.root, exist_ok=True)
        except OSError as exc:
            logger.error(
                "Cannot create model root %s: %s. If Mini-Dock is the "
                "target, verify /Volumes/Mini-Dock is mounted.",
                args.root, exc,
            )
            return 2

    failures: list[str] = []
    for engine in engines:
        try:
            fetch(engine, args.model, args.root, args.force)
        except Exception as exc:
            logger.error("%s fetch failed: %s", engine, exc)
            failures.append(engine)

    if failures:
        logger.error("Failures: %s", ", ".join(failures))
        return 1
    logger.info("All models fetched to %s", args.root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
