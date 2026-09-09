from fingate.agent.grounding import GroundingReport, check_grounding


def _facts(**kwargs) -> list[dict]:
    return [kwargs]


def test_an_answer_with_no_numbers_is_grounded():
    report = check_grounding("연동 계열이 함께 움직였다.", tool_results=[], prompt_text="")

    assert report.ok
    assert report.ungrounded == []


def test_a_number_returned_by_a_tool_is_grounded():
    report = check_grounding(
        "스프레드 표준편차는 0.0593이다.",
        tool_results=_facts(spread_sd=0.0593),
        prompt_text="",
    )

    assert report.ok


def test_a_number_no_tool_returned_is_flagged():
    """이게 전부다. 도구가 주지 않은 수치를 말하면 잡는다."""
    report = check_grounding(
        "기준금리는 3.75로 인상됐다.",
        tool_results=_facts(spread_sd=0.0593),
        prompt_text="",
    )

    assert not report.ok
    assert "3.75" in report.ungrounded


def test_rounding_a_returned_value_is_still_grounded():
    """도구가 15.083을 주고 모델이 15.1σ라고 쓰는 것은 환각이 아니다."""
    report = check_grounding(
        "스프레드가 15.1σ 벗어났다.",
        tool_results=_facts(z=15.083),
        prompt_text="",
    )

    assert report.ok


def test_rounding_tolerance_does_not_swallow_a_real_difference():
    """15.083을 15.9로 쓰는 것은 반올림이 아니다."""
    report = check_grounding(
        "스프레드가 15.9σ 벗어났다.",
        tool_results=_facts(z=15.083),
        prompt_text="",
    )

    assert not report.ok


def test_a_ratio_stated_as_a_percentage_is_grounded():
    """도구는 0.922를 주고 사람은 92.2%로 읽는다. % 기호가 붙은 경우만 허용한다."""
    report = check_grounding(
        "동행률은 92.2%다.", tool_results=_facts(agreement=0.922), prompt_text=""
    )

    assert report.ok


def test_a_bare_number_is_not_grounded_by_the_percent_rule():
    """% 없이 92.2라고 쓰면 0.922의 변형으로 인정하지 않는다."""
    report = check_grounding(
        "동행률은 92.2이다.", tool_results=_facts(agreement=0.922), prompt_text=""
    )

    assert not report.ok


def test_thousands_separators_are_grounded():
    report = check_grounding(
        "자산총계는 160,146,430,758,072원이다.",
        tool_results=_facts(total_assets=160146430758072),
        prompt_text="",
    )

    assert report.ok


def test_an_iso_date_is_matched_as_a_date_not_as_three_numbers():
    """날짜를 숫자 셋으로 쪼개면 2026·09·04가 각각 근거를 요구하게 된다."""
    report = check_grounding(
        "2026-09-04 관측이 문제다.",
        tool_results=_facts(period="2026-09-04"),
        prompt_text="",
    )

    assert report.ok


def test_a_date_no_tool_returned_is_flagged():
    report = check_grounding(
        "2030-01-01 관측이 문제다.",
        tool_results=_facts(period="2026-09-04"),
        prompt_text="",
    )

    assert not report.ok
    assert "2030-01-01" in report.ungrounded


def test_a_number_from_the_question_is_grounded_and_labelled():
    """승인자가 질문에 쓴 수치를 되받는 것은 지어내는 것이 아니다.

    다만 출처가 도구인지 질문인지 구분되어야 한다. 질문은 검증된 근거가 아니다.
    """
    report = check_grounding(
        "말씀하신 0.9 급변은 확인되지 않는다.",
        tool_results=[],
        prompt_text="0.9 급변이 진짜인가?",
    )

    assert report.ok
    assert report.sources["0.9"] == "prompt"


def test_a_tool_number_is_labelled_as_tool():
    report = check_grounding("표준편차 0.0593이다.", tool_results=_facts(sd=0.0593), prompt_text="")

    assert report.sources["0.0593"] == "tool"


def test_numbers_nested_deep_in_tool_results_are_found():
    """probe 의 facts 는 중첩 구조다. 얕게 훑으면 정상 수치를 환각으로 몬다."""
    results = [{"anchors": [{"spread_sd": 0.0593, "meta": {"overlap": 900}}]}]

    report = check_grounding("겹침 900, 표준편차 0.0593.", tool_results=results, prompt_text="")

    assert report.ok


def test_a_negative_number_is_matched_with_its_sign():
    report = check_grounding(
        "스프레드 평균은 -0.2135다.", tool_results=_facts(mean=-0.2135), prompt_text=""
    )

    assert report.ok


def test_the_report_lists_every_ungrounded_number_not_just_the_first():
    report = check_grounding("3.75와 4.20 모두 확인된다.", tool_results=[], prompt_text="")

    assert set(report.ungrounded) == {"3.75", "4.20"}


def test_the_report_is_a_typed_result_not_a_bool():
    report = check_grounding("표준편차 0.0593.", tool_results=_facts(sd=0.0593), prompt_text="")

    assert isinstance(report, GroundingReport)
    assert report.checked >= 1
