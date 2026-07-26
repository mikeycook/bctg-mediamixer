import sys
from pathlib import Path

# Pipeline modules are flat at the repository root, matching the sibling
# genAITest convention.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
