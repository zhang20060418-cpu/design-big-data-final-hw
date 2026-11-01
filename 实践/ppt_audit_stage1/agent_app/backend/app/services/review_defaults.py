"""Resolve API-aware new-task defaults without overriding explicit opt-out."""
from ..schemas import ReviewOptions


def api_configured(app_settings: dict, api_key: str) -> bool:
    return all(str(value or '').strip() for value in (
        app_settings.get('api_base_url'),app_settings.get('api_model'),api_key))


def resolve_review_defaults(options: ReviewOptions, app_settings: dict, api_key: str) -> ReviewOptions:
    """Only omitted fields receive defaults; saved/manual False stays False.

    API readiness is configuration presence, not a fabricated connection test.
    Normal defaults never select Mock or remove the existing call/page budgets.
    """
    if options.mllm_user_choice is not None:
        return options.model_copy(update={'enable_mllm': options.mllm_user_choice})
    if 'mllm_user_choice' not in options.model_fields_set and 'enable_mllm' in options.model_fields_set:
        # Legacy clients explicitly sent this boolean. Persist that intention
        # so a later read/start can distinguish opt-out from an old default.
        return options.model_copy(update={'mllm_user_choice': options.enable_mllm})
    enabled=api_configured(app_settings,api_key) and options.review_mode!='off'
    updates={'enable_mllm':enabled}
    if enabled and options.review_strategy is None:
        updates['review_strategy']='selective_review'
    return options.model_copy(update=updates)
