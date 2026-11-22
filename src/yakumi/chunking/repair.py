"""边界修复：发现高风险边界，截取局部音频重新识别，只在结果确实更好时才替换。

风险信号（逐个边界检查，范围为切点前后 context 秒）：
- gap        VAD 判定有语音的地方连续超过 min_gap 秒没有任何词（疑似漏字）
- duplicate  切点两侧出现相同的 n-gram（疑似重复）
- merge      forced 边界的 LCS 拼接失败（合并阶段已标记）
- timing     词时长超过 3 秒、相邻词重叠超过 0.2 秒等时间异常
- truncated  窗口末尾被截断、且已拆到最短（识别阶段已标记）

修复：截取连续的原始音频 [t − context, t + context]，重新识别；两端各留 edge 秒不用
（那里的词可能被片段边缘切断），再用合并阶段的时间约束 LCS 与原有词序列拼接。

采纳规则：同一段音频两次识别的结果本来就会不同，所以不能因为“不一样”就认为原结果错了。
只看结构性指标：目标问题要有改善，其他指标不能变差，两端拼接都要成功；否则保留原结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import media
from ..asr.base import ASRError, ASRProvider
from ..asr.types import AudioWindow, Word
from ..text import CJK_RE, join_texts
from .merge import clean_words, midpoint, splice, to_units

Region = tuple[float, float]
PRIORITY = {"merge": 0, "truncated": 1, "gap": 2, "duplicate": 3, "timing": 4}


@dataclass
class Issue:
    kinds: list[str]
    time: float  # 问题中心（切点）
    region: Region  # 计划重新识别的范围
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def priority(self) -> int:
        return min(PRIORITY[k] for k in self.kinds)


@dataclass
class Score:
    uncovered: float  # 未被词覆盖的语音时长（秒）
    duplicates: int
    anomalies: int

    def to_dict(self) -> dict[str, Any]:
        return {"uncovered": round(self.uncovered, 2), "duplicates": self.duplicates, "anomalies": self.anomalies}


# ---- 检测 -------------------------------------------------------------------
def _merge_intervals(items: list[Region]) -> list[Region]:
    out: list[Region] = []
    for s, e in sorted(items):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def uncovered_speech(words: list[Word], speech: list[Region], lo: float, hi: float,
                     pad: float = 0.25, min_len: float = 0.8) -> list[Region]:
    """[lo, hi] 内有语音、但前后 pad 秒内都没有词的区间（只返回不短于 min_len 的）。"""
    covered = _merge_intervals([(w.start - pad, w.end + pad) for w in words if w.end >= lo - pad and w.start <= hi + pad])
    gaps: list[Region] = []
    for s, e in speech:
        s, e = max(s, lo), min(e, hi)
        if e <= s:
            continue
        cursor = s
        for cs, ce in covered:
            if ce <= cursor or cs >= e:
                continue
            if cs > cursor:
                gaps.append((cursor, cs))
            cursor = max(cursor, ce)
        if cursor < e:
            gaps.append((cursor, e))
    return [(s, e) for s, e in gaps if e - s >= min_len]


def _ngrams(words: list[Word]) -> list[tuple[str, float]]:
    """(n-gram, 时间)：拉丁文字 3 个词，中日韩文字 5 个字。"""
    units = to_units(words)
    out = []
    for i in range(len(units)):
        n = 5 if CJK_RE.search(units[i].key) else 3
        if i + n <= len(units):
            out.append(("".join(u.key for u in units[i:i + n]), units[i].time))
    return out


def duplicate_count(words: list[Word], t: float, span: float = 2.0) -> int:
    """切点左侧 span 秒内的 n-gram 在右侧 span 秒内再次出现的次数。"""
    grams = _ngrams([w for w in words if t - span <= midpoint(w) <= t + span])
    left = {g for g, tt in grams if tt < t}
    return sum(1 for g, tt in grams if tt >= t and g in left)


def timing_anomalies(words: list[Word], lo: float, hi: float, max_word: float = 3.0,
                     max_overlap: float = 0.2) -> int:
    near = [w for w in words if lo <= midpoint(w) <= hi]
    count = sum(1 for w in near if w.end - w.start > max_word)
    count += sum(1 for a, b in zip(near, near[1:]) if a.end - b.start > max_overlap)
    return count


def score(words: list[Word], speech: list[Region], issue: Issue, cfg: dict[str, Any]) -> Score:
    lo, hi = issue.region
    gaps = uncovered_speech(words, speech, lo, hi, min_len=float(cfg.get("min_gap_seconds", 0.8)))
    return Score(sum(e - s for s, e in gaps), duplicate_count(words, issue.time), timing_anomalies(words, lo, hi))


def detect(words: list[Word], speech: list[Region], duration: float, boundaries: list[dict[str, Any]],
           truncated: list[AudioWindow], cfg: dict[str, Any]) -> list[Issue]:
    ctx = float(cfg.get("context", 6.0))
    min_gap = float(cfg.get("min_gap_seconds", 0.8))
    issues: list[Issue] = []
    for b in boundaries:
        t = float(b["time"])
        region = (max(0.0, t - ctx), min(duration, t + ctx))
        kinds = []
        if b.get("risky"):
            kinds.append("merge")
        if uncovered_speech(words, speech, *region, min_len=min_gap):
            kinds.append("gap")
        if duplicate_count(words, t) > 0:
            kinds.append("duplicate")
        if timing_anomalies(words, *region) > 0:
            kinds.append("timing")
        if kinds:
            issues.append(Issue(kinds, t, region, {"boundary": b}))
    for w in truncated:
        last = max((x.end for x in words if w.core_start <= midpoint(x) <= w.core_end), default=w.core_start)
        region = (max(0.0, last - 2.0), min(duration, w.core_end + 1.0))
        issues.append(Issue(["truncated"], last, region, {"window": w.index}))
    issues.sort(key=lambda i: (i.priority, i.time))
    return issues


# ---- 拼接 -------------------------------------------------------------------
def _zone_words(words: list[Word], lo: float, hi: float) -> int:
    return sum(1 for w in words if lo <= midpoint(w) <= hi)


def apply_repair(words: list[Word], repaired: list[Word], region: Region, duration: float,
                 edge: float = 1.0, zone: float = 2.0, tolerance: float = 0.5) -> list[Word] | None:
    """把 region 内重新识别的词（全局时间）拼回原序列；两端拼接失败时返回 None。"""
    lo, hi = region
    if hi - lo < 2 * (edge + zone):
        return None
    # 左端：原序列 → 修复结果
    if lo <= 0:
        before, replacement_right = range(0), range(len(repaired))
    else:
        z_lo, z_hi = lo + edge, lo + edge + zone
        cut = (z_lo + z_hi) / 2
        a_win = AudioWindow(-1, 0, z_hi, 0, cut, None, "forced")
        b_win = AudioWindow(-2, z_lo, hi, cut, hi, "forced", None)
        before, replacement_right, rep = splice(words, repaired, a_win, b_win, tolerance)
        if rep.strategy != "lcs" and (_zone_words(words, z_lo, z_hi) or _zone_words(repaired, z_lo, z_hi)):
            return None
    # 右端：修复结果 → 原序列
    if hi >= duration:
        replacement_left, after = range(len(repaired)), range(0)
    else:
        z_lo, z_hi = hi - edge - zone, hi - edge
        cut = (z_lo + z_hi) / 2
        a_win = AudioWindow(-2, lo, z_hi, lo, cut, None, "forced")
        b_win = AudioWindow(-1, z_lo, duration, cut, duration, "forced", None)
        replacement_left, after, rep = splice(repaired, words, a_win, b_win, tolerance)
        if rep.strategy != "lcs" and (_zone_words(words, z_lo, z_hi) or _zone_words(repaired, z_lo, z_hi)):
            return None
    if ((replacement_left and replacement_right and replacement_right[0] > replacement_left[-1] + 1)
            or (before and after and before[-1] >= after[0])):
        return None
    replacement = set(replacement_left).intersection(replacement_right)
    return clean_words([words[i] for i in before] + [repaired[i] for i in sorted(replacement)] + [words[i] for i in after])


def accept(issue: Issue, before: Score, after: Score) -> tuple[bool, str]:
    if after.uncovered > before.uncovered + 0.3:
        return False, "漏字变多"
    if after.duplicates > before.duplicates:
        return False, "重复变多"
    if after.anomalies > before.anomalies:
        return False, "时间异常变多"
    improved = (
        ("gap" in issue.kinds or "truncated" in issue.kinds) and after.uncovered < before.uncovered - 0.3
        or "duplicate" in issue.kinds and after.duplicates < before.duplicates
        or "timing" in issue.kinds and after.anomalies < before.anomalies
        # 合并失败的边界：修复片段连续覆盖了切点，两端拼接成功即可采用
        or "merge" in issue.kinds
    )
    return (True, "已改善") if improved else (False, "没有改善")


# ---- 执行 -------------------------------------------------------------------
class Repairer:
    def __init__(self, asr: ASRProvider, audio: Path, work: Path, speech: list[Region], duration: float,
                 cfg: dict[str, Any], language: str | None, stats: dict[str, Any],
                 clip_suffix: str = ".flac", tolerance: float = 0.5,
                 report: Callable[[float, str], None] | None = None):
        self.asr = asr
        self.audio = audio
        self.work = work
        self.speech = speech
        self.duration = duration
        self.cfg = cfg
        self.language = language
        self.stats = stats
        self.clip_suffix = clip_suffix
        self.tolerance = tolerance
        self.report = report or (lambda f, m: None)

    def run(self, words: list[Word], boundaries: list[dict[str, Any]],
            truncated: list[AudioWindow]) -> tuple[list[Word], list[dict[str, Any]]]:
        issues = detect(words, self.speech, self.duration, boundaries, truncated, self.cfg)
        budget = int(self.cfg.get("max_repairs", 20))
        log: list[dict[str, Any]] = []
        done: list[Region] = []
        for k, issue in enumerate(issues):
            entry: dict[str, Any] = {"time": round(issue.time, 3), "kinds": issue.kinds,
                                     "region": [round(x, 3) for x in issue.region]}
            if any(issue.region[0] < e and s < issue.region[1] for s, e in done):
                entry.update(accepted=False, reason="与已修复的区域重叠，跳过")
                log.append(entry)
                continue
            if budget <= 0:
                entry.update(accepted=False, reason="超出修复次数上限")
                log.append(entry)
                continue
            budget -= 1
            self.report(k / max(1, len(issues)), f"边界复核 {k + 1}/{len(issues)}")
            before = score(words, self.speech, issue, self.cfg)
            clip = media.extract_clip(self.audio, *issue.region, self.work / f"repair{self.clip_suffix}")
            try:
                lo, hi = issue.region
                raw = self.asr.transcribe(clip, self.language, AudioWindow(-1, lo, hi, lo, hi))
            except ASRError as exc:
                self._count(None, failed=True)
                entry.update(accepted=False, reason=f"重新识别失败：{exc}")
                log.append(entry)
                continue
            finally:
                clip.unlink(missing_ok=True)
            self._count(raw.cost)
            entry["cost"] = raw.cost
            repaired = [w.shifted(issue.region[0]) for w in raw.words]
            new_words = apply_repair(words, repaired, issue.region, self.duration, tolerance=self.tolerance)
            if new_words is None:
                entry.update(accepted=False, reason="两端拼接失败", before=before.to_dict())
                log.append(entry)
                continue
            after = score(new_words, self.speech, issue, self.cfg)
            ok, reason = accept(issue, before, after)
            entry.update(accepted=ok, reason=reason, before=before.to_dict(), after=after.to_dict())
            lo, hi = issue.region
            entry["original_text"] = _text(words, lo, hi)
            entry["repaired_text"] = _text(new_words, lo, hi)
            if ok:
                words = new_words
                done.append(issue.region)
            log.append(entry)
        # repairs_*：本次运行的结果；repair_requests / repair_cost 跨运行累计（钱确实花了）
        self.stats["repairs_found"] = len(issues)
        self.stats["repairs_tried"] = sum(1 for e in log if "before" in e or "重新识别失败" in e.get("reason", ""))
        self.stats["repairs_accepted"] = sum(1 for e in log if e.get("accepted"))
        return words, log

    def _count(self, cost: float | None, failed: bool = False) -> None:
        s = self.stats
        s["repair_requests"] = s.get("repair_requests", 0) + 1
        if failed:
            s["repair_failed"] = s.get("repair_failed", 0) + 1
        elif cost is None:
            s["asr_cost_unknown"] = s.get("asr_cost_unknown", 0) + 1
        else:
            s["repair_cost"] = round(s.get("repair_cost", 0.0) + cost, 6)


def _text(words: list[Word], lo: float, hi: float) -> str:
    return join_texts([w.text for w in words if lo <= midpoint(w) <= hi])
