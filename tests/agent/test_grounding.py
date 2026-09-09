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


# --- 실모델 산문에서 드러난 오탐 세 종류 (2026-09-09 실호출) -------------------


def test_truncating_instead_of_rounding_is_not_a_hallucination():
    """도구가 0.014538 을 주고 모델이 "0.014σ" 라고 썼다. 버림도 정직한 표기다.

    반올림만 인정하면 0.015 만 통과하고 0.014 는 환각으로 몰린다.
    """
    report = check_grounding(
        "스프레드가 0.014σ 수준으로 유지됐다.",
        tool_results=_facts(z=0.014538953878225442),
        prompt_text="",
    )

    assert report.ok


def test_widening_for_truncation_does_not_let_a_wrong_value_through():
    """마지막 자리 하나만 넓힌다. 0.014538 을 0.019 로 쓰는 것은 여전히 잡는다."""
    report = check_grounding(
        "스프레드가 0.019σ다.", tool_results=_facts(z=0.014538953878225442), prompt_text=""
    )

    assert not report.ok


def test_a_field_name_the_tool_returned_is_not_a_numeric_claim():
    """모델이 "p90" 이라고 쓰면 90 이 수치 주장처럼 잡힌다.

    noise_p90 은 도구가 반환한 **키 이름**이다. 값이 아니라 이름을 인용한 것이다.
    """
    report = check_grounding(
        "허용 노이즈 수준(p90: 약 0.063)을 넘지 않았다.",
        tool_results=_facts(noise_p90=0.063),
        prompt_text="",
    )

    assert report.ok


def test_markdown_list_numbering_is_not_a_measurement():
    """모델은 근거를 번호 목록으로 쓴다. 그 번호가 수치 주장으로 잡히면 안 된다.

    실호출에서 1과 3은 우연히 통과하고 2만 잡혔다. 그 자체가 잡음이라는 증거다.
    """
    answer = "1. 원본이 없다.\n2. 연동 계열이 미동조했다.\n3. 승인이 취소됐다."

    report = check_grounding(answer, tool_results=[], prompt_text="")

    assert report.ok, f"목록 번호가 수치로 잡혔다: {report.ungrounded}"


def test_a_bold_markdown_list_marker_is_also_ignored():
    report = check_grounding("**2.** 두 번째 근거다.", tool_results=[], prompt_text="")

    assert report.ok


def test_a_number_in_prose_is_still_checked_even_next_to_a_list():
    """목록 번호를 무시하되 본문의 수치는 계속 검사해야 한다."""
    report = check_grounding("1. 기준금리는 4.25다.", tool_results=[], prompt_text="")

    assert not report.ok
    assert "4.25" in report.ungrounded


# --- 실모델 답변 6건에서 드러난 날짜 표기 (2026-09-09) -----------------------


def test_a_korean_date_is_matched_as_a_date():
    """모델은 ISO 가 아니라 "2026년 9월 5일" 로 쓴다.

    쪼개면 연도와 월은 우연히 통과하고 일자만 잡힌다. 실측 6건이 전부 이 형태였다.
    """
    report = check_grounding(
        "2026년 9월 5일 자 데이터가 문제다.",
        tool_results=_facts(period="2026-09-05"),
        prompt_text="",
    )

    assert report.ok, f"한국어 날짜가 쪼개져 잡혔다: {report.ungrounded}"


def test_a_korean_date_the_tools_never_returned_is_flagged():
    report = check_grounding(
        "2030년 1월 1일에 발생했다.", tool_results=_facts(period="2026-09-05"), prompt_text=""
    )

    assert not report.ok


def test_a_korean_year_month_is_matched():
    report = check_grounding(
        "2024년 12월 기준이다.", tool_results=_facts(period="2024-12-31"), prompt_text=""
    )

    assert report.ok


def test_a_korean_month_day_without_a_year_is_matched():
    report = check_grounding(
        "9월 5일 관측이다.", tool_results=_facts(period="2026-09-05"), prompt_text=""
    )

    assert report.ok


def test_a_month_day_that_no_date_has_is_flagged():
    report = check_grounding(
        "3월 14일 관측이다.", tool_results=_facts(period="2026-09-05"), prompt_text=""
    )

    assert not report.ok


def test_a_dotted_date_is_matched():
    """DART 는 기간을 2024.01.01 형태로 표기한다. 모델이 그대로 옮길 수 있다."""
    report = check_grounding(
        "2024.12.31 기준이다.", tool_results=_facts(period="2024-12-31"), prompt_text=""
    )

    assert report.ok


def test_a_date_does_not_ground_unrelated_numbers():
    """날짜를 인정한다고 그 안의 숫자가 다른 자리에서까지 근거가 되면 안 된다."""
    report = check_grounding(
        "2026년 9월 5일이고 금리는 5다.",
        tool_results=_facts(period="2026-09-05"),
        prompt_text="",
    )

    assert not report.ok
    assert "5" in report.ungrounded


# --- 식별자 (2026-09-09 실측: 모델이 exception_id 를 인용한다) ----------------


UID = "c4e54b3d-550c-4a9b-b8a3-f2b3cfa88eff"


def test_a_uuid_is_recorded_as_one_token_not_as_digit_fragments():
    """모델은 차단 건 id 를 그대로 인용한다. 숫자 추출기가 그것을 조각낸다.

    실측에서 UUID 하나가 9개의 "근거 없는 수치" 로 잡혔다. 하이픈을 음수 부호로
    읽어 -550 까지 나왔다. 조각이 우연히 맞아 통과하는 것으로는 부족하다 —
    **식별자 전체가 하나의 토큰으로 처리되어야 한다.**
    """
    report = check_grounding(
        f"차단 건({UID})의 측정 데이터다.", tool_results=_facts(exception_id=UID), prompt_text=""
    )

    assert report.ok
    assert UID in report.sources, "식별자가 통째로 대조되지 않았다"
    assert not any(frag in report.sources for frag in ("54", "-550", "88"))


def test_an_identifier_whose_fragments_exist_is_still_flagged_as_a_whole():
    """조각이 전부 도구 반환값에 있어도 그 식별자 자체가 없으면 잡아야 한다.

    조각 단위로 근거를 인정하면 지어낸 식별자가 통과한다.
    """
    report = check_grounding(
        "차단 건(f2b3cfa88eff-550c-4a9b-b8a3-c4e54b3d)이다.",
        tool_results=_facts(exception_id=UID),
        prompt_text="",
    )

    assert not report.ok


def test_a_series_identifier_with_digits_is_matched_whole():
    """dart:00113058, 722Y001, INFO-100 도 같은 문제를 만든다.

    숫자가 없는 코드(req-demo)는 애초에 쪼개질 것이 없어 보호가 필요 없다.
    """
    for code in ("dart:00113058", "722Y001", "INFO-100"):
        report = check_grounding(
            f"식별자는 {code}다.", tool_results=_facts(code=code), prompt_text=""
        )
        assert report.ok, f"{code} 가 쪼개져 잡혔다: {report.ungrounded}"
        assert code in report.sources, f"{code} 가 통째로 대조되지 않았다"


def test_a_code_without_digits_needs_no_protection():
    report = check_grounding(
        "식별자는 req-demo다.", tool_results=_facts(code="req-demo"), prompt_text=""
    )

    assert report.ok


def test_an_identifier_does_not_ground_a_separate_number():
    """식별자를 인정한다고 그 안의 숫자가 다른 자리에서까지 근거가 되면 안 된다."""
    report = check_grounding(
        f"건 {UID} 이고 금리는 550이다.", tool_results=_facts(exception_id=UID), prompt_text=""
    )

    assert not report.ok
    assert "550" in report.ungrounded


def test_a_date_is_still_a_date_not_an_identifier():
    """날짜는 글자가 없으므로 식별자 규칙에 먹히면 안 된다."""
    report = check_grounding(
        "2026-09-05 관측이다.", tool_results=_facts(period="2026-09-05"), prompt_text=""
    )

    assert report.ok
    assert "2026-09-05" in report.sources


def test_an_iso_timestamp_is_matched_whole():
    """도구는 시각을 ISO 로 반환하고 모델은 그대로 옮긴다.

    날짜만 통째로 다루면 뒤의 시·분·초와 마이크로초가 조각난다. 실측에서
    2026-09-08T00:55:13.508517+00:00 의 508517 이 근거 없음으로 잡혔다.
    """
    stamp = "2026-09-08T00:55:13.508517+00:00"
    report = check_grounding(
        f"직전 스냅샷({stamp} 기준)으로 서빙된다.",
        tool_results=_facts(last_good_at=stamp),
        prompt_text="",
    )

    assert report.ok, f"타임스탬프가 조각나 잡혔다: {report.ungrounded}"
    assert stamp in report.sources


def test_a_timestamp_the_tools_never_returned_is_flagged():
    report = check_grounding(
        "직전 스냅샷(2030-01-01T09:00:00+00:00)이다.",
        tool_results=_facts(last_good_at="2026-09-08T00:55:13.508517+00:00"),
        prompt_text="",
    )

    assert not report.ok


def test_a_reformatted_timestamp_is_still_matched():
    """모델은 표에 넣으려고 T 와 마이크로초·타임존을 떼고 다시 쓴다.

    실측: 도구 2026-09-08T00:55:13.508517+00:00 -> 모델 "2026-09-08 00:55:13".
    """
    report = check_grounding(
        "직전 스냅샷은 2026-09-08 00:55:13 이다.",
        tool_results=_facts(last_good_at="2026-09-08T00:55:13.508517+00:00"),
        prompt_text="",
    )

    assert report.ok, f"재포맷한 시각이 잡혔다: {report.ungrounded}"


def test_a_reformat_does_not_let_a_different_time_through():
    report = check_grounding(
        "직전 스냅샷은 2026-09-08 09:12:44 이다.",
        tool_results=_facts(last_good_at="2026-09-08T00:55:13.508517+00:00"),
        prompt_text="",
    )

    assert not report.ok
