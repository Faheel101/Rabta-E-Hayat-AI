"""Entry point for synthetic network generation.

The generator itself lives in `datagen/` (spec §16.2). This module stays as the
documented command so the pipeline in scripts/rebuild.py and any existing
runbook keeps working.

    python -m scripts.generate_synthetic
"""

from datagen.run import main

if __name__ == "__main__":
    main()
