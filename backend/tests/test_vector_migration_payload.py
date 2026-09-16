"""Migration may tolerate PDF coordinate roundoff, never altered source content."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("vector_migration", Path(__file__).parents[2] / "scripts/migrate-qdrant-service.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_pdf_bbox_roundoff_is_explicitly_bounded():
    a = {"text": "完整原文", "content_sha256": "frozen", "locator": {"bbox": [107.31579999999997]}}
    b = {"text": "完整原文", "content_sha256": "frozen", "locator": {"bbox": [107.31579999999995]}}
    assert 0 < module.verify_payload(a, b) < 1e-9


@pytest.mark.parametrize("a,b", [
    ({"text": "完整"}, {"text": "缺失"}),
    ({"content_sha256": "a"}, {"content_sha256": "b"}),
    ({"ordinal": 1}, {"ordinal": 1.0}),
    ({"locator": {"bbox": [1.]}}, {"locator": {"bbox": [1.00001]}}),
    ({"locator": {"bbox": [1.]}}, {"locator": {"bbox": [float("nan")]}}),
    ({"value": 1.}, {"value": 1.0000000000001}),
    ({"x": None}, {}),
    ({"locator": {"bbox": [1., 2.]}}, {"locator": {"bbox": [1.]}}),
])
def test_other_payload_differences_fail_closed(a, b):
    with pytest.raises(RuntimeError, match="MIGRATION_PAYLOAD"):
        module.verify_payload(a, b)
