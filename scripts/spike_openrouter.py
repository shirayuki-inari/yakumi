"""实测 OpenRouter 转录接口：词级时间戳、处理耗时与片段长度的关系、费用。

用法：
    python scripts/spike_openrouter.py AUDIO OUT_DIR [--durations 30,60,120] [--offset 30]
        [--language ja] [--format verbose_json|json] [--model MODEL]

密钥与 base_url 从 Yakumi 配置读取。每次请求的原始响应保存到 OUT_DIR，
汇总打印为一行 JSON。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import httpx

from yakumi.config import load_config


def cut(audio: Path, start: float, duration: float, out: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(duration), "-i", str(audio),
         "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", "-c:a", "flac", str(out)],
        check=True,
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--durations", default="30")
    ap.add_argument("--offset", type=float, default=30.0)
    ap.add_argument("--language", default=None)
    ap.add_argument("--format", default="verbose_json")
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args()

    cfg = load_config()["asr"]
    model = args.model or cfg["model"]
    url = cfg["base_url"].rstrip("/") + "/audio/transcriptions"
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    args.out_dir.mkdir(parents=True, exist_ok=True)

    for dur in [float(d) for d in args.durations.split(",")]:
        tag = f"{args.audio.stem}_at{int(args.offset)}_{int(dur)}s_{args.format}"
        clip = cut(args.audio, args.offset, dur, args.out_dir / f"{tag}.flac")
        data: dict = {"model": model, "response_format": args.format}
        if args.format == "verbose_json":
            data["timestamp_granularities[]"] = ["word", "segment"]
        if args.language:
            data["language"] = args.language
        t0 = time.monotonic()
        try:
            with open(clip, "rb") as f:
                resp = httpx.post(url, data=data, files={"file": (clip.name, f, "audio/flac")},
                                  headers=headers, timeout=args.timeout)
            elapsed = time.monotonic() - t0
            status, text = resp.status_code, resp.text
        except httpx.HTTPError as exc:
            elapsed, status, text = time.monotonic() - t0, None, f"{type(exc).__name__}: {exc}"
        (args.out_dir / f"{tag}.response.txt").write_text(text, encoding="utf-8")

        summary: dict = {"clip": tag, "seconds": dur, "bytes": clip.stat().st_size, "status": status,
                         "elapsed": round(elapsed, 2)}
        try:
            body = json.loads(text)
        except ValueError:
            body = None
        if isinstance(body, dict) and status == 200:
            words = body.get("words") or []
            segs = body.get("segments") or []
            summary.update(
                keys=sorted(body),
                language=body.get("language"),
                duration=body.get("duration"),
                chars=len(body.get("text") or ""),
                n_words=len(words),
                n_segments=len(segs),
                first_words=words[:5],
                last_word=words[-1] if words else None,
                first_segment=segs[0] if segs else None,
                usage=body.get("usage"),
            )
        else:
            summary["error"] = text[:500]
        print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
