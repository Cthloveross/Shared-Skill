import concurrent.futures

import pytest

from scripts.wildclawbench.local_budget import LocalRequestBudget, BudgetExceeded


def config(tmp_path, requests=4, tokens=400):
    return {'context_length': 100, 'local_request_budget': {
        'path': str(tmp_path / 'budget.json'), 'max_requests': requests, 'max_tokens': tokens}}


def test_local_budget_parallel_admission_and_restart(tmp_path):
    cfg = config(tmp_path)

    def reserve(_):
        try:
            LocalRequestBudget(cfg).reserve()
            return True
        except BudgetExceeded:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(12))) == 4
    with pytest.raises(BudgetExceeded):
        LocalRequestBudget(cfg).reserve()


def test_token_reservations_can_stop_before_request_limit(tmp_path):
    cfg = config(tmp_path, tokens=150)
    assert LocalRequestBudget(cfg).reserve()['reserved_tokens'] == 100
    with pytest.raises(BudgetExceeded):
        LocalRequestBudget(cfg).reserve()
    with pytest.raises(ValueError, match='checkpoint changed'):
        LocalRequestBudget(config(tmp_path, tokens=200)).reserve()


def test_local_budget_opt_in_and_path_validation(tmp_path):
    assert LocalRequestBudget({}).reserve() is None
    cfg = config(tmp_path)
    cfg['local_request_budget']['path'] = 'relative.json'
    with pytest.raises(ValueError, match='absolute'):
        LocalRequestBudget(cfg)


def test_media_budget_shared_across_roles_parallel_and_restarts(tmp_path):
    from scripts.wildclawbench.local_budget import MediaRequestBudget
    cfg = {'media_request_budget': {'path': str(tmp_path / 'media.json'), 'max_requests': 3}}
    def reserve(i):
        try:
            MediaRequestBudget(cfg).reserve('audio' if i % 2 else 'image')
            return True
        except BudgetExceeded:
            return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(12))) == 3
    with pytest.raises(BudgetExceeded):
        MediaRequestBudget(cfg).reserve('audio')
    with pytest.raises(ValueError, match='corrupt'):
        MediaRequestBudget({'media_request_budget': {**cfg['media_request_budget'], 'max_requests': 4}}).reserve('image')


def test_nested_media_limit_is_preserved_in_frozen_public_configuration():
    from scripts.wildclawbench.protocol import public_config
    cfg = {'auxiliary_limits': {'max_audio_tokens': 2048}, 'local_api_key': 'secret'}
    assert public_config(cfg) == {'auxiliary_limits': {'max_audio_tokens': 2048}}
