"""AI 生成 PPT 质量审查系统 —— 统一结论结构。

本模块是《缺陷体系与标注规范》第 6 节的代码实现，作为全部检测器、
融合层与报告层的共同接口。修改字段前请先改规范文档，再改此处。

两层结构：
    Claim   检测器原始判决（present / absent），用于冲突检测与覆盖率统计
    Finding 融合后的结论原子，进入报告与实验指标

典型用法：
    claims = rule_detector(prs) + ml_detector(features) + llm_detector(pages)
    findings = fuse(claims, rule_version="0.1")
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence


class Severity(str, Enum):
    BLOCKING = "blocking"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


class Status(str, Enum):
    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    NEEDS_REVIEW = "needs_review"


class ConfidenceSource(str, Enum):
    RULE = "rule"
    MODEL_PROB = "model_prob"
    LLM_REPORT = "llm_report"
    FUSED = "fused"


class Verdict(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"


class EvidenceKind(str, Enum):
    STRUCTURAL = "structural"
    VISUAL = "visual"
    SEMANTIC = "semantic"
    REQUIREMENT = "requirement"


DEFECT_CODES = (
    "S-OVERSHOOT", "S-TEXT-OVERFLOW", "S-FONT-FALLBACK", "S-PAGECOUNT",
    "S-EMPTY", "S-DENSITY", "S-DECO-COUNT",
    "V-WHITESPACE", "V-ALIGN", "V-HIERARCHY", "V-COLOR", "V-TEMPLATE-REPEAT",
    "M-CROSSPAGE", "M-IMAGE-TEXT", "M-DECO-FUNC",
    "R-CONTENT-MISS", "R-STYLE-DEVIATE", "R-SCOPE",
)

# 检测路由：由本表决定，不由编码前缀推断。
# 须与 01_标准 3.2 表「首选层」列保持一致；两者不同步时以本文档为准并立即修正。
# 只列已确定路由的缺陷码，二批中未定路由的不在此表。
PREFERRED_LAYER = {
    "S-OVERSHOOT": "L1",
    "S-TEXT-OVERFLOW": "L1",
    "S-FONT-FALLBACK": "L1",
    "S-PAGECOUNT": "L1",
    "S-DECO-COUNT": "L1",
    "V-WHITESPACE": "L2",
    "V-TEMPLATE-REPEAT": "L2",
    "V-HIERARCHY": "L1",      # 例外：视觉现象，但最小证据是字号属性，无需渲染图
    "M-CROSSPAGE": "L3",
    "R-CONTENT-MISS": "L3",
}

_SEVERITY_ORDER = {
    Severity.BLOCKING: 3,
    Severity.MAJOR: 2,
    Severity.MINOR: 1,
    Severity.INFO: 0,
}


def family_of(defect_code: str) -> str:
    """返回现象族前缀 S / V / M / R。

    仅用于编码校验与报告分组，**不用于检测路由**——路由查 PREFERRED_LAYER。
    两者可以不一致：`V-HIERARCHY` 属视觉族，但路由到 L1 规则层。
    """
    if defect_code not in DEFECT_CODES:
        raise ValueError(f"未知缺陷编码: {defect_code}")
    return defect_code[0]


def route_of(defect_code: str) -> str:
    """返回该缺陷码的检测层 L1 / L2 / L3。未定路由时抛错，避免静默猜测。"""
    if defect_code not in DEFECT_CODES:
        raise ValueError(f"未知缺陷编码: {defect_code}")
    if defect_code not in PREFERRED_LAYER:
        raise ValueError(f"缺陷 {defect_code} 尚未确定检测路由，请在 PREFERRED_LAYER 中登记")
    return PREFERRED_LAYER[defect_code]


def severity_rank(sev: Severity | str) -> int:
    return _SEVERITY_ORDER[Severity(sev)]


def bbox_iou(a: Optional[Sequence[float]], b: Optional[Sequence[float]]) -> float:
    """归一化 bbox 的 IoU。任一为空时视为同一页级单元，返回 1.0。"""
    if a is None or b is None:
        return 1.0 if (a is None and b is None) else 0.0
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


@dataclass
class Evidence:
    kind: EvidenceKind | str
    ref: str
    value: Any = None
    expected: Any = None
    note: str = ""

    def validate(self) -> None:
        if not self.ref:
            raise ValueError("Evidence.ref 不能为空，每条结论都必须可回溯")
        EvidenceKind(self.kind)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = EvidenceKind(self.kind).value
        return d


@dataclass
class Claim:
    """检测器原始判决。absent 也要输出，否则冲突与覆盖率无法统计。"""

    detector: str
    slide_index: int
    defect_code: str
    verdict: Verdict | str = Verdict.PRESENT
    score: float = 0.0
    bbox: Optional[tuple] = None
    severity: Severity | str = Severity.MINOR
    source: ConfidenceSource | str = ConfidenceSource.RULE
    rationale: str = ""
    suggestion: str = ""
    evidence: list = field(default_factory=list)
    cost_calls: int = 0
    cost_tokens: int = 0

    def validate(self) -> None:
        if self.slide_index < 1:
            raise ValueError("slide_index 从 1 开始")
        family_of(self.defect_code)
        Verdict(self.verdict)
        Severity(self.severity)
        ConfidenceSource(self.source)
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score 须在 [0,1]: {self.score}")
        if self.bbox is not None and len(self.bbox) != 4:
            raise ValueError("bbox 须为 [x, y, w, h]")
        if self.evidence:
            for ev in self.evidence:
                ev.validate()


@dataclass
class Finding:
    """融合后的结论原子。字段与 finding_schema.json 一一对应。"""

    finding_id: str
    slide_index: int
    defect_code: str
    detector: str
    severity: Severity | str
    status: Status | str
    confidence: float
    confidence_source: ConfidenceSource | str
    evidence: list
    rationale: str
    suggestion: str = ""
    bbox: Optional[tuple] = None
    element_ids: list = field(default_factory=list)
    cost_calls: int = 0
    cost_tokens: int = 0
    dedup_group: str = ""
    rule_version: str = ""
    detectors_checked: list = field(default_factory=list)
    conflict_detectors: list = field(default_factory=list)

    def validate(self) -> None:
        if self.slide_index < 1:
            raise ValueError("slide_index 从 1 开始")
        family_of(self.defect_code)
        Severity(self.severity)
        Status(self.status)
        ConfidenceSource(self.confidence_source)
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence 须在 [0,1]: {self.confidence}")
        if not self.evidence:
            raise ValueError("空证据记录不允许存在")
        for ev in self.evidence:
            ev.validate()
        if not self.rationale:
            raise ValueError("rationale 不能为空")
        if not self.rule_version:
            raise ValueError("rule_version 不能为空，否则结果不可复现")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = Severity(self.severity).value
        d["status"] = Status(self.status).value
        d["confidence_source"] = ConfidenceSource(self.confidence_source).value
        d["evidence"] = [ev.to_dict() for ev in self.evidence]
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _cluster(claims: list[Claim], min_iou: float) -> list[list[Claim]]:
    """按 bbox IoU 聚簇；bbox 为空的页级主张单独成簇。"""
    clusters: list[list[Claim]] = []
    for c in claims:
        for cluster in clusters:
            if bbox_iou(c.bbox, cluster[0].bbox) > min_iou:
                cluster.append(c)
                break
        else:
            clusters.append([c])
    return clusters


def fuse(
    claims: Sequence[Claim],
    rule_version: str,
    min_iou: float = 0.5,
) -> list[Finding]:
    """把多检测器 Claim 融合为 Finding。

    规则见规范 6.5：
      - 严重度取簇内最高
      - 存在 present 与 absent 分歧 → needs_review
      - 全部 present 且均无分歧 → confirmed 或 probable（按分数带宽）
      - 无 present 的簇不产出 Finding
    """
    grouped: dict[tuple, list[Claim]] = {}
    for c in claims:
        c.validate()
        grouped.setdefault((c.slide_index, c.defect_code), []).append(c)

    findings: list[Finding] = []
    counter: dict[tuple, int] = {}

    for (slide_index, code), group in sorted(grouped.items()):
        checked = sorted({c.detector for c in group})
        for cluster in _cluster(group, min_iou):
            present = [c for c in cluster if Verdict(c.verdict) is Verdict.PRESENT]
            absent = [c for c in cluster if Verdict(c.verdict) is Verdict.ABSENT]
            if not present:
                continue

            lead = max(present, key=lambda c: c.score)
            severity = max((Severity(c.severity) for c in present), key=severity_rank)
            confidence = max(c.score for c in present)

            key = (code, slide_index)
            counter[key] = counter.get(key, 0) + 1
            seq = counter[key]

            if absent:
                status = Status.NEEDS_REVIEW
                rationale = lead.rationale
                source = ConfidenceSource.FUSED
            elif confidence < 0.40:
                status = Status.NEEDS_REVIEW
                rationale = lead.rationale
                source = ConfidenceSource.FUSED
            elif len(present) > 1:
                # 多源一致才叫"融合置信度"。单源判定不标 FUSED：
                # 无论它走规则还是模型，置信度来源就是它自己（lead.source）。
                # v0.3 前此处曾用 family_of(code) == "S" 推断"规则可确定"，
                # 但 V-HIERARCHY 属视觉族却走规则层，该假设会把单源规则判定误标为 FUSED。
                status = Status.CONFIRMED
                rationale = lead.rationale
                source = ConfidenceSource.FUSED
            elif confidence >= 0.60:
                status = Status.CONFIRMED
                rationale = lead.rationale
                source = ConfidenceSource(lead.source)
            else:
                status = Status.PROBABLE
                rationale = lead.rationale
                source = ConfidenceSource(lead.source)

            seen: set[tuple] = set()
            merged: list[Evidence] = []
            for c in present:
                for ev in c.evidence:
                    k = (EvidenceKind(ev.kind).value, ev.ref)
                    if k not in seen:
                        seen.add(k)
                        merged.append(ev)

            findings.append(
                Finding(
                    finding_id=f"{code}-{slide_index}-{seq:02d}",
                    slide_index=slide_index,
                    defect_code=code,
                    detector="+".join(checked) if len(checked) > 1 else lead.detector,
                    severity=severity,
                    status=status,
                    confidence=round(confidence, 4),
                    confidence_source=source,
                    evidence=merged,
                    rationale=rationale,
                    suggestion=lead.suggestion,
                    bbox=lead.bbox,
                    cost_calls=sum(c.cost_calls for c in cluster),
                    cost_tokens=sum(c.cost_tokens for c in cluster),
                    dedup_group=f"{slide_index}:{code}:{seq}",
                    rule_version=rule_version,
                    detectors_checked=checked,
                    conflict_detectors=sorted({c.detector for c in absent}),
                )
            )

    return findings


def coverage(claims: Sequence[Claim]) -> dict[str, set[str]]:
    """统计每个检测器在哪些缺陷类别上做过判断，用于实验二的覆盖率指标。"""
    table: dict[str, set[str]] = {}
    for c in claims:
        table.setdefault(c.detector, set()).add(c.defect_code)
    return table


def _self_test() -> None:
    ev_struct = Evidence(
        kind=EvidenceKind.STRUCTURAL,
        ref="slide.7.shape.23",
        value={"x": 12.4, "w": 3.2, "unit": "cm"},
        expected={"x_max": 13.33},
    )
    ev_visual = Evidence(
        kind=EvidenceKind.VISUAL,
        ref="slide.7.region.[0.92,0.10,0.14,0.20]",
        value="foreground_ratio=0.83",
    )

    claims = [
        Claim(
            detector="rule.geometry@0.1",
            slide_index=7,
            defect_code="S-OVERSHOOT",
            verdict=Verdict.PRESENT,
            score=1.0,
            bbox=(0.92, 0.10, 0.14, 0.20),
            severity=Severity.BLOCKING,
            rationale="第 7 页右下角图片超出页面右边界 1.2cm",
            suggestion="将图片宽度收至 12.1cm 或整体左移",
            evidence=[ev_struct],
        ),
        Claim(
            detector="ml.layout@0.1",
            slide_index=7,
            defect_code="S-OVERSHOOT",
            verdict=Verdict.PRESENT,
            score=0.71,
            bbox=(0.90, 0.11, 0.15, 0.19),
            severity=Severity.MAJOR,
            source=ConfidenceSource.MODEL_PROB,
            rationale="视觉模型认为该区域存在越界",
            evidence=[ev_visual],
            cost_calls=1,
            cost_tokens=820,
        ),
        Claim(
            detector="llm.vision@0.1",
            slide_index=7,
            defect_code="S-OVERSHOOT",
            verdict=Verdict.ABSENT,
            score=0.20,
            bbox=(0.92, 0.10, 0.14, 0.20),
            source=ConfidenceSource.LLM_REPORT,
            rationale="未观察到越界",
            cost_calls=1,
            cost_tokens=1450,
        ),
    ]

    findings = fuse(claims, rule_version="0.1")
    assert len(findings) == 1, findings
    f = findings[0]
    f.validate()

    assert f.status is Status.NEEDS_REVIEW, "存在分歧时应降级为待核实"
    assert f.severity is Severity.BLOCKING, "严重度应取簇内最高"
    assert len(f.evidence) == 2, "证据应去重合并"
    assert f.cost_tokens == 2270
    assert f.detectors_checked == ["llm.vision@0.1", "ml.layout@0.1", "rule.geometry@0.1"]
    assert f.conflict_detectors == ["llm.vision@0.1"], "分歧来源须单独记录，供报告展示"

    cov = coverage(claims)
    assert cov["rule.geometry@0.1"] == {"S-OVERSHOOT"}
    assert len(cov) == 3

    # v0.3：编码前缀不再等于检测路由，两者解耦
    assert family_of("V-HIERARCHY") == "V", "族前缀保持不变"
    assert route_of("V-HIERARCHY") == "L1", "视觉族但走规则层——这是解耦的关键例证"
    assert route_of("M-CROSSPAGE") == "L3"
    assert route_of("V-WHITESPACE") == "L2"
    entered = {
        "S-OVERSHOOT", "S-TEXT-OVERFLOW", "S-FONT-FALLBACK", "S-PAGECOUNT",
        "S-DECO-COUNT", "V-WHITESPACE", "V-TEMPLATE-REPEAT", "M-CROSSPAGE",
        "R-CONTENT-MISS",
    }
    missing = entered - set(PREFERRED_LAYER)
    assert not missing, f"首批缺陷缺路由登记: {missing}"

    print(f.to_json())
    print("\n自检通过：冲突降级、严重度取最高、证据去重、成本累加、覆盖率统计、路由表完整性均正常。")


if __name__ == "__main__":
    _self_test()
