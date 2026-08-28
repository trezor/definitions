import pytest

from .common import SIGNATURES_REQUIRED
from .crypto import _combine_public_key


@pytest.mark.parametrize("version", sorted(SIGNATURES_REQUIRED))
def test_combine_public_key_accepts_required_key_count(version):
    sigmask = (1 << SIGNATURES_REQUIRED[version]) - 1
    assert _combine_public_key(sigmask, version)


@pytest.mark.parametrize("version", sorted(SIGNATURES_REQUIRED))
def test_combine_public_key_rejects_too_few_keys(version):
    sigmask = (1 << (SIGNATURES_REQUIRED[version] - 1)) - 1
    with pytest.raises(AssertionError):
        _combine_public_key(sigmask, version)


def test_v1_requires_two_keys_v2_one_key():
    # one key set in sigmask
    assert _combine_public_key(0b001, 2)
    with pytest.raises(AssertionError):
        _combine_public_key(0b001, 1)
    # two keys set in sigmask work for both
    assert _combine_public_key(0b011, 1)
    assert _combine_public_key(0b011, 2)
