"""产品层冻结引擎封装。"""

from hikbox_pictures.product.engine.frozen_v5 import late_fusion_similarity, run_frozen_v5_assignment
from hikbox_pictures.product.engine.param_snapshot import FROZEN_V5_PARAM_SNAPSHOT, build_frozen_v5_param_snapshot

__all__ = [
    "FROZEN_V5_PARAM_SNAPSHOT",
    "build_frozen_v5_param_snapshot",
    "late_fusion_similarity",
    "run_frozen_v5_assignment",
]
