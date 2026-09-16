"""Hash-bound, separately labelled administrator-confirmed review."""
from . import admin_review
from . import models as m
from . import services as svc
from .api_retention import _operation

SCHEMAS = {
    "AdminReviewInput": {"type": "object", "additionalProperties": False,
        "required": ["reviewed_sha256", "reason", "confirm_valid_sources", "acknowledge_not_independent"],
        "properties": {"reviewed_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "reason": {"type": "string", "minLength": 8, "maxLength": 1000, "pattern": r"\S"},
            "confirm_valid_sources": {"const": True}, "acknowledge_not_independent": {"const": True}}},
    "AdminReviewStatus": {"type": "object"},
}
PATHS = {"/versions/{id}/admin-review": {
    "get": _operation("getAdminReview", "AdminReviewStatus"),
    "post": _operation("confirmAdminReview", "AdminReviewStatus", "AdminReviewInput")}}
for operation in PATHS["/versions/{id}/admin-review"].values():
    operation["tags"] = ["Administrator confirmation"]


def status(ctx):
    v = svc.version_access(ctx.db, ctx.user, ctx.id)
    can_confirm = True
    try:
        admin_review.require_manager(ctx.db, ctx.user, v)
    except svc.APIError:
        can_confirm = False
    return svc.tagged({"version_id": v.id, "can_confirm": can_confirm,
        "current_sha256": svc.check_frozen_hash(ctx.db, v),
        "confirmation": admin_review.confirmation(ctx.db, v)}, v)


def replay_authority(ctx, cached):
    v = ctx.db.get(m.ResourceVersion, ctx.id)
    if not v:
        svc.fail(404, "NOT_FOUND", "对象不存在或不可访问")
    admin_review.require_manager(ctx.db, ctx.user, v)
    record = admin_review.confirmation(ctx.db, v)
    if not record or svc.check_frozen_hash(ctx.db, v) != record["content_sha256"]:
        svc.fail(409, "ADMIN_CONFIRMATION_STALE", "当前确认已失效")


HANDLERS = {"getAdminReview": status, "confirmAdminReview": admin_review.approve}
