from argon2 import PasswordHasher as Argon2Hasher

from backend.services.passwords import PasswordHasher
from tests.api_support import fast_hasher


def test_hash_is_argon2id_and_verifies() -> None:
    hasher = fast_hasher()
    encoded = hasher.hash("correct horse battery")
    assert encoded.startswith("$argon2id$")
    assert hasher.verify(encoded, "correct horse battery")
    assert not hasher.verify(encoded, "wrong password!!")


def test_hash_is_salted() -> None:
    hasher = fast_hasher()
    assert hasher.hash("same password 123") != hasher.hash("same password 123")


def test_malformed_hash_never_verifies() -> None:
    assert not fast_hasher().verify("not-a-hash", "whatever password")


def test_needs_rehash_for_weaker_parameters() -> None:
    weak = PasswordHasher(Argon2Hasher(time_cost=1, memory_cost=8, parallelism=1))
    strong = PasswordHasher(Argon2Hasher(time_cost=2, memory_cost=16, parallelism=1))
    old = weak.hash("correct horse battery")
    assert strong.needs_rehash(old)
    assert not weak.needs_rehash(old)
    assert not strong.needs_rehash(strong.hash("correct horse battery"))


def test_default_parameters_use_argon2id() -> None:
    assert PasswordHasher().hash("correct horse battery").startswith("$argon2id$")


def test_verify_dummy_does_not_raise() -> None:
    fast_hasher().verify_dummy("anything at all")
