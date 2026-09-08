"""권고 규칙 — 근거를 하나의 제안으로 합친다.

이것은 판단이 아니라 **명시된 규칙**이다. 규칙이 코드에 적혀 있어야 나중에
"왜 이렇게 권고했는가"에 답할 수 있다. 같은 근거에 같은 권고가 나온다.

권고는 결정이 아니다. 어떤 경로로도 원장을 바꾸지 못한다. 승인은 사람이
사유를 적어야만 이뤄진다.
"""

from dataclasses import dataclass
from enum import StrEnum

from .probes import Evidence, ProbeVerdict


class Recommendation(StrEnum):
    """원장 상태값(approved/rejected)과 절대 같은 단어를 쓰지 않는다.

    같은 단어를 쓰면 감사 로그에서 에이전트의 제안과 사람의 결정을 구분할 수 없다.
    """

    APPROVE_LIKELY = "approve_likely"
    REJECT_LIKELY = "reject_likely"
    REJECT = "reject_pipeline_defect"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class Recommended:
    verdict: Recommendation
    because: str


def _verdict_of(evidence: list[Evidence], probe: str) -> ProbeVerdict | None:
    for item in evidence:
        if item.probe == probe:
            return item.verdict
    return None


def recommend(evidence: list[Evidence]) -> Recommended:
    """우선순위대로 적용한다. 순서가 규칙의 전부다.

    1. 원본 불일치 -> REJECT. 자기 결함은 승인하지 않는다. 다른 근거를 덮어쓴다.
    2. 연동 계열 미동행 -> REJECT_LIKELY
    3. 연동 계열 동행 -> APPROVE_LIKELY
    4. 그 외 -> INSUFFICIENT_EVIDENCE

    1이 2·3보다 위에 있는 것이 핵심이다. 뒤집히면 peer가 강하게 동행하는
    상황에서 우리 파이프라인이 만들어낸 값이 통과한다.
    """
    # 소스마다 원본 대조 probe 이름이 다르다. 둘 중 무엇이든 원본 불일치면
    # 같은 결론이다 — 우리가 만든 결함은 승인 대상이 아니다.
    for probe in ("raw_provenance", "financial_provenance"):
        if _verdict_of(evidence, probe) is ProbeVerdict.SUPPORTS_DEFECT:
            return Recommended(
                Recommendation.REJECT,
                f"{probe}: 적재된 값이 원본 응답과 다르다. 출처 문제가 아니라 "
                "파이프라인 결함이므로 승인 대상이 아니다.",
            )

    peer = _verdict_of(evidence, "peer_corroboration")
    if peer is ProbeVerdict.SUPPORTS_DEFECT:
        return Recommended(
            Recommendation.REJECT_LIKELY,
            "peer_corroboration: 연동된 계열이 함께 움직이지 않았다. 이 계열만 튀었다.",
        )
    if peer is ProbeVerdict.SUPPORTS_REAL:
        return Recommended(
            Recommendation.APPROVE_LIKELY,
            "peer_corroboration: 연동된 계열이 같은 방향으로 함께 움직였다. 실제 변동으로 보인다.",
        )

    return Recommended(
        Recommendation.INSUFFICIENT_EVIDENCE,
        "방향을 가릴 근거가 없다. 권고를 지어내지 않는다. 승인자가 직접 판단해야 한다.",
    )
