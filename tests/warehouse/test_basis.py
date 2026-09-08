import pytest

from fingate.warehouse.indicators import (
    CONSOLIDATED,
    SEPARATE,
    resolve_indicators,
)

REPORT = "11011"


def _row(account: str, amount: str, fs_div: str, sj_div: str = "BS") -> dict:
    return {
        "sj_div": sj_div,
        "account_nm": account,
        "thstrm_amount": amount,
        "fs_div": fs_div,
    }


def _both_bases() -> list[dict]:
    """DART는 같은 계정을 연결·별도 두 벌로 보낸다. 실측 56/56 보고서가 그랬다."""
    return [
        _row("자산총계", "160", CONSOLIDATED),
        _row("부채총계", "146", CONSOLIDATED),
        _row("자본총계", "14", CONSOLIDATED),
        _row("자산총계", "122", SEPARATE),
        _row("부채총계", "112", SEPARATE),
        _row("자본총계", "10", SEPARATE),
    ]


def test_the_declared_basis_wins_not_the_response_order():
    """마지막에 온 행이 이기면 응답 순서가 데이터의 의미를 정하게 된다.

    실측에서 마지막-승자 규칙은 238/238 전부 별도재무제표를 골랐다.
    아무도 그렇게 정하지 않았고 어디에도 기록되지 않았다.
    """
    resolved, _ = resolve_indicators(REPORT, _both_bases(), basis=CONSOLIDATED)

    assets = [i for i in resolved if i.indicator_id == "total_assets"]
    assert len(assets) == 1, "한 지표에 한 값만 남아야 한다"
    assert assets[0].amount == 160
    assert assets[0].fs_div == CONSOLIDATED


def test_the_other_basis_is_requested_explicitly():
    resolved, _ = resolve_indicators(REPORT, _both_bases(), basis=SEPARATE)

    assets = next(i for i in resolved if i.indicator_id == "total_assets")
    assert assets.amount == 122
    assert assets.fs_div == SEPARATE


def test_every_resolved_indicator_records_which_basis_it_came_from():
    """기록하지 않으면 나중에 연결인지 별도인지 알 방법이 없다."""
    resolved, _ = resolve_indicators(REPORT, _both_bases(), basis=CONSOLIDATED)

    assert {i.fs_div for i in resolved} == {CONSOLIDATED}


def test_rows_of_the_other_basis_are_not_silently_discarded():
    """조용히 버리면 왜 그 지표가 비었는지 나중에 추적할 수 없다."""
    _, unmapped = resolve_indicators(REPORT, _both_bases(), basis=CONSOLIDATED)

    assert any(item.reason == "OTHER_BASIS" for item in unmapped)
    assert sum(1 for item in unmapped if item.reason == "OTHER_BASIS") == 3


def test_a_response_without_the_declared_basis_yields_nothing():
    """없는 기준을 다른 기준으로 조용히 대체하지 않는다. 품질 계약이 결측으로 잡는다."""
    only_separate = [r for r in _both_bases() if r["fs_div"] == SEPARATE]

    resolved, unmapped = resolve_indicators(REPORT, only_separate, basis=CONSOLIDATED)

    assert resolved == []
    assert all(item.reason == "OTHER_BASIS" for item in unmapped)


def test_a_row_without_a_basis_field_is_kept():
    """fs_div 가 없는 응답 형태에서도 동작해야 한다. 기존 테스트 픽스처가 그렇다."""
    resolved, _ = resolve_indicators(
        REPORT,
        [{"sj_div": "BS", "account_nm": "자산총계", "thstrm_amount": "100"}],
        basis=CONSOLIDATED,
    )

    assert len(resolved) == 1
    assert resolved[0].fs_div == ""


@pytest.mark.parametrize("basis", [CONSOLIDATED, SEPARATE])
def test_each_basis_balances_on_its_own(basis):
    """두 기준이 각자 균형을 이루므로 항등식만으로는 혼입을 잡을 수 없다.

    실측에서 항등식이 50/50 성립했던 이유다. 옳아서가 아니라
    어느 쪽이 이겼든 그쪽 안에서는 맞았기 때문이다.
    """
    resolved, _ = resolve_indicators(REPORT, _both_bases(), basis=basis)
    amounts = {i.indicator_id: i.amount for i in resolved}

    assert amounts["total_assets"] == amounts["total_liabilities"] + amounts["total_equity"]
