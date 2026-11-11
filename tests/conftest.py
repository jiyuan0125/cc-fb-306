"""让 tests/ 下的测试能够导入仓库根目录的 dim_builder / query 模块。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
