import json
import pytest

from fund_kb.retrieval_profile import read_local_profile, prepared_development_settings
from fund_kb.settings import Settings


def write_profile(root,**updates):
    root.mkdir(exist_ok=True)
    values={"schema_version":1,"embedding_model":"BAAI/bge-small-zh-v1.5","embedding_dimensions":512,
        "embedding_model_path":str(root/"model"),"embedding_cache_dir":str(root/"cache"),"embedding_revision":"public-fixed-revision"}
    values.update(updates)
    path=root/"retrieval-profile.json";path.write_text(json.dumps(values))
    return path


def test_explicit_local_profile_is_nonsecret_and_keeps_downloads_disabled(tmp_path):
    profile=write_profile(tmp_path)
    settings=Settings(storage_dir=tmp_path,retrieval_profile=profile)
    assert settings.retrieval_mode == "hybrid" and settings.embedding_mode == "fastembed"
    assert settings.embedding_dimensions == 512 and settings.embedding_allow_downloads is False
    assert settings.embedding_api_key is None


@pytest.mark.parametrize("updates",[{"embedding_api_key":"synthetic-forbidden-field"},
    {"embedding_model_path":"/outside-app/model"},{"embedding_dimensions":0},
    {"embedding_chunk_bytes":64},{"embedding_mode":"http"},{"embedding_allow_downloads":True}])
def test_local_profile_rejects_credentials_external_paths_and_invalid_budgets(tmp_path,updates):
    path=write_profile(tmp_path,**updates)
    with pytest.raises(ValueError,match="LOCAL_RETRIEVAL_PROFILE_INVALID"):
        read_local_profile(path,tmp_path)


def test_explicit_wiki_mode_is_not_overridden_by_prepared_local_profile(tmp_path):
    write_profile(tmp_path)
    explicit=Settings(app_env="development",storage_dir=tmp_path,retrieval_mode="wiki")
    assert prepared_development_settings(explicit).retrieval_mode == "wiki"
    implicit=Settings(app_env="development",storage_dir=tmp_path)
    assert prepared_development_settings(implicit).retrieval_mode == "hybrid"
