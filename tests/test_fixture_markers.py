"""Fixture と pytest marker の整合性を保証する universal regression test (#229).

bulk vault のような重量 fixture を使うテストは必ず ``@pytest.mark.slow`` を
持たなければならない。marker を忘れると ``pytest -m 'not slow'`` でも fixture が
build されてしまい、「slow を skip して高速実行する」という design intent が
silent に壊れる。本テストは collection 時に全 item を走査して付け忘れを検知する
(#169 で導入した bulk_vault_over_cap の follow-up)。

新たな重量 fixture を追加した際は ``BULK_FIXTURE_NAMES`` に名前を登録すること。
"""

from __future__ import annotations

import pytest

# @pytest.mark.slow の付与を強制される fixture 名の集合。
# 追加時は同名 fixture が conftest 側で session-scoped であることを確認する。
BULK_FIXTURE_NAMES: frozenset[str] = frozenset({"bulk_vault_over_cap"})


def test_bulk_fixture_users_are_marked_slow(request: pytest.FixtureRequest) -> None:
    """重量 fixture を使う全テストが ``@pytest.mark.slow`` を持つ.

    collection 済みの session.items を走査し、``BULK_FIXTURE_NAMES`` を
    fixturenames に含むが ``slow`` marker を持たない item を検出する。
    新規テストが付け忘れた場合に fail する regression guard。
    """
    violations: list[str] = []
    for item in request.session.items:
        fixtures = set(getattr(item, "fixturenames", ()))
        if not fixtures & BULK_FIXTURE_NAMES:
            continue
        marker_names = {m.name for m in item.iter_markers()}
        if "slow" not in marker_names:
            violations.append(item.nodeid)
    assert not violations, (
        f"重量 fixture {sorted(BULK_FIXTURE_NAMES)} を使うが @pytest.mark.slow が欠落: {violations}"
    )
