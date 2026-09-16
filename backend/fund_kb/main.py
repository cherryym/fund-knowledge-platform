"""Application integration: API, persistent jobs, Qdrant and development corpus."""
from __future__ import annotations

from contextlib import ExitStack, asynccontextmanager

from sqlalchemy import select

from .api import create_app
from .settings import Settings, get_settings


def build_application(settings: Settings | None = None):
    settings = settings or get_settings()
    from .retrieval_profile import prepared_development_settings
    # Application-owned runtime caches/bridges must not leak through a caller's
    # Settings instance when it is used to build another application.
    settings = prepared_development_settings(settings).model_copy()
    application = create_app(settings)
    original_lifespan = application.router.lifespan_context

    @asynccontextmanager
    async def integrated_lifespan(app):
        async with original_lifespan(app):
            from .codex_host import configured_bridge
            from .jobs import JobDispatcher
            from .seed import seed_demo

            def close_owned(name, resource):
                try:
                    resource.close()
                finally:
                    if getattr(app.state, name, None) is resource:
                        setattr(app.state, name, None)
                    if name == "codex_bridge" and getattr(settings, "_codex_bridge", None) is resource:
                        object.__setattr__(settings, "_codex_bridge", None)

            # Register ownership immediately: partial startup and an individual
            # close failure must still release every other acquired resource.
            with ExitStack() as cleanup:
                bridge = getattr(app.state, "codex_bridge", None)
                if bridge is None:
                    bridge = configured_bridge(settings)
                    app.state.codex_bridge = bridge
                    if bridge is not None:
                        cleanup.callback(close_owned, "codex_bridge", bridge)
                object.__setattr__(settings, "_codex_bridge", bridge)
                object.__setattr__(settings, "_codex_session_factory", app.state.session_factory)

                vector = getattr(app.state, "vector_index", None)
                if vector is None and settings.retrieval_mode == "hybrid":
                    from .retrieval import VectorIndex
                    vector = VectorIndex(settings)
                    app.state.vector_index = vector
                    cleanup.callback(close_owned, "vector_index", vector)
                from .retrieval_registry import registry_for
                registry = registry_for(settings, vector)
                app.state.retrieval_registry = registry
                if registry is not None:
                    cleanup.callback(close_owned, "retrieval_registry", registry)
                    if registry.resolve().vector is not vector:
                        raise RuntimeError("RETRIEVAL_DEFAULT_CONFIGURATION_MISMATCH")
                dispatcher = JobDispatcher(settings, app.state.session_factory, vector,
                    codex_bridge=bridge, retrieval_registry=registry)
                app.state.job_dispatcher = dispatcher
                cleanup.callback(close_owned, "job_dispatcher", dispatcher)
                app.state.seed_result = seed_demo(app.state.session_factory, settings, vector)
                if settings.app_env == "development" and vector is not None and settings.embedding_mode == "hashing":
                    _index_development(app.state.session_factory, vector)
                dispatcher.recover()
                yield

    application.router.lifespan_context = integrated_lifespan

    return application


def _index_development(session_factory, vector):
    from .models import ContentBlock, Release, Resource, ResourceVersion
    with session_factory() as session:
        records = []
        for resource in session.scalars(select(Resource).where(Resource.deleted_at.is_(None),
                                                                   Resource.suspended.is_(False))):
            for version in session.scalars(select(ResourceVersion).where(
                ResourceVersion.resource_id == resource.id, ResourceVersion.state == "APPROVED")):
                if not session.scalars(select(Release).where(Release.version_id == version.id,
                                                              Release.state.in_(["ACTIVE", "SUPERSEDED"]))).first():
                    continue
                for block in session.scalars(select(ContentBlock).where(ContentBlock.version_id == version.id)):
                    records.append({"resource_id": resource.id, "version_id": version.id,
                                    "block_id": block.block_id, "title": version.title,
                                    "text": block.search_text, "locator": block.locator or {},
                                    "content_sha256": block.content_sha256,
                                    "block_type": block.block_type, "data": block.data,
                                    "kind": resource.kind, "knowledge_type": version.knowledge_type,
                                    "required_facts": version.required_facts or [],
                                    "applicability": version.applicability or {},
                                    "resource_access_epoch": resource.access_epoch})
        if records:
            vector.upsert(records)


app = build_application()
