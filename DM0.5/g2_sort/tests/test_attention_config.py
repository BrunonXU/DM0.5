from types import SimpleNamespace

from opendm.exp.dm05_exp import DM05ModelConfig


def _fake_dm05_config():
    return SimpleNamespace(
        vlm_config=SimpleNamespace(_attn_implementation=None),
        action_config=SimpleNamespace(_attn_implementation=None),
    )


def test_attention_recipe_is_applied_before_model_construction():
    recipe = DM05ModelConfig(
        llm_attn_implementation="flex_attention",
        vision_attn_implementation="flash_attention_2",
        action_attn_implementation="sdpa",
    )
    config = _fake_dm05_config()

    recipe._apply_construction_attention_options(config)

    assert config.vlm_config._attn_implementation == {
        "": "flex_attention",
        "text_config": "flex_attention",
        "vision_config": "flash_attention_2",
    }
    assert config.action_config._attn_implementation == "sdpa"


def test_auto_uses_sdpa_for_safe_construction():
    recipe = DM05ModelConfig(
        llm_attn_implementation="auto",
        vision_attn_implementation="auto",
        action_attn_implementation="auto",
    )
    config = _fake_dm05_config()

    recipe._apply_construction_attention_options(config)

    assert config.vlm_config._attn_implementation == {
        "": "sdpa",
        "text_config": "sdpa",
        "vision_config": "sdpa",
    }
    assert config.action_config._attn_implementation == "sdpa"
