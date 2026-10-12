"""以本批目录和题号前缀复用已有脱敏导出，不调用模型或修改任务。"""
from pathlib import Path

DOC = Path(__file__).resolve().parent
source_path = DOC.parent / "swebench-pro-40-batch3-2026-10-11/publish.py"
source = source_path.read_text()
for previous, current in (
    ("nz-swebench-pro-v2-40-batch3-2026-10-11", "nz-swebench-pro-v2-20-batch4-2026-10-12"),
    ("swebench-pro-40-batch3-2026-10-11", "swebench-pro-20-batch4-2026-10-12"),
    ("b3-", "b4-"),
):
    if previous not in source:
        raise RuntimeError("existing_exporter_batch_parameter_changed")
    source = source.replace(previous, current)
# 本批最后零调用题的 p3 位于内存阶段之后，保留原件并按真实先后取终态。
previous_order = "('attempts','preparation-blocked','preparation-retry-attempts','final-unattempted','supplement-attempts','memory-attempts')"
current_order = "('attempts','preparation-blocked','preparation-retry-attempts','supplement-attempts','memory-attempts','final-unattempted')"
if source.count(previous_order) != 1:
    raise RuntimeError("existing_exporter_stage_order_changed")
source = source.replace(previous_order, current_order)
exec(compile(source, str(source_path), "exec"), {"__name__": "__main__"})
