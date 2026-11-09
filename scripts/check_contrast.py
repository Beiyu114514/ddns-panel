#!/usr/bin/env python3
"""对比度门禁：解析 app/static/style.css 的明/暗主题令牌并计算 WCAG 对比度。

用法：
    .venv/bin/python scripts/check_contrast.py
    .venv/bin/python scripts/check_contrast.py --json

阈值：正文/文字 4.5:1；控件边界、焦点环、图表与图形 3:1。
装饰性分隔线 --border-muted 不属于控件边界，单独标注为豁免。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CSS_PATH = Path(__file__).resolve().parent.parent / "app" / "static" / "style.css"

TEXT_PAIRS = [
    ("正文", "text", "bg"),
    ("正文", "text", "surface"),
    ("正文", "text", "surface-2"),
    ("辅助文字", "muted", "bg"),
    ("辅助文字", "muted", "surface"),
    ("辅助文字", "muted", "surface-2"),
    ("链接", "accent", "surface"),
    ("链接", "accent", "bg"),
    ("成功徽标", "ok", "ok-soft"),
    ("成功徽标", "ok", "surface"),
    ("危险文字", "danger", "danger-soft"),
    ("危险文字", "danger", "surface"),
    ("警告徽标", "warn", "warn-soft"),
    ("警告徽标", "warn", "surface"),
    ("主按钮文字", "on-accent", "accent-solid"),
    ("主按钮文字", "on-accent", "accent-solid-hover"),
    ("危险按钮文字", "on-danger", "danger-solid"),
    ("危险按钮文字", "on-danger", "danger-solid-hover"),
    ("类型徽标", "accent", "accent-soft"),
]
GRAPHIC_PAIRS = [
    ("控件边框", "border", "surface"),
    ("控件边框", "border", "bg"),
    ("焦点环", "focus", "surface"),
    ("图表折线(检查)", "accent", "surface"),
    ("图表折线(异常)", "danger", "surface"),
    ("图表折线(成功)", "ok", "surface"),
] + [(f"时间线色块 {i}", f"ip-{i}", "surface") for i in range(1, 13)]


def _rgb(value: str) -> tuple[int, int, int]:
    h = value.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) == 8:
        h = h[:6]
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _lum(rgb: tuple[int, int, int]) -> float:
    def channel(v: int) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    l1, l2 = _lum(_rgb(fg)), _lum(_rgb(bg))
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def load_themes(css: str) -> dict[str, dict[str, str]]:
    dark_at = css.find("@media (prefers-color-scheme: dark)")
    if dark_at < 0:
        raise SystemExit("找不到深色主题 @media 块")
    light_block = css[:dark_at]
    dark_block = css[dark_at:]

    def grab(block: str) -> dict[str, str]:
        found = re.search(r":root\s*\{(.*?)\}", block, re.S)
        if not found:
            raise SystemExit("找不到 :root 令牌块")
        return {m.group(1): m.group(2).lower()
                for m in re.finditer(r"--([\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8})", found.group(1))}

    return {"浅色": grab(light_block), "深色": grab(dark_block)}


def run() -> tuple[list[dict], list[str]]:
    themes = load_themes(CSS_PATH.read_text(encoding="utf-8"))
    results: list[dict] = []
    errors: list[str] = []
    for theme_name, tokens in themes.items():
        for label, fg, bg in TEXT_PAIRS:
            _check(results, errors, theme_name, label, tokens, fg, bg, 4.5)
        for label, fg, bg in GRAPHIC_PAIRS:
            _check(results, errors, theme_name, label, tokens, fg, bg, 3.0)
        muted = tokens.get("border-muted")
        if muted:
            results.append({
                "theme": theme_name, "label": "装饰分隔线（豁免）",
                "fg": muted, "bg": tokens["surface"],
                "ratio": round(contrast(muted, tokens["surface"]), 2),
                "min": 0, "pass": True, "exempt": True,
            })
    return results, errors


def _check(results, errors, theme, label, tokens, fg, bg, minimum):
    if fg not in tokens or bg not in tokens:
        errors.append(f"{theme} 缺少令牌 --{fg} 或 --{bg}")
        return
    ratio = contrast(tokens[fg], tokens[bg])
    ok = ratio + 1e-9 >= minimum
    results.append({
        "theme": theme, "label": label, "fg": tokens[fg], "bg": tokens[bg],
        "ratio": round(ratio, 2), "min": minimum, "pass": ok, "exempt": False,
    })
    if not ok:
        errors.append(f"{theme} / {label}: {tokens[fg]} on {tokens[bg]} = {ratio:.2f}:1"
                      f"（要求 ≥ {minimum}:1）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()
    results, errors = run()
    if args.json:
        print(json.dumps({"results": results, "errors": errors}, ensure_ascii=False, indent=2))
    else:
        for theme in ("浅色", "深色"):
            print(f"\n[{theme}主题]")
            for r in results:
                if r["theme"] != theme:
                    continue
                mark = "PASS" if r["pass"] else "FAIL"
                if r["exempt"]:
                    mark = "SKIP"
                print(f"  {mark:4} {r['label']:<16} {r['fg']} on {r['bg']:<10}"
                      f" {r['ratio']:5.2f}:1 (≥{r['min']}:1)")
        print()
    if errors:
        print("\n对比度不达标：", file=sys.stderr)
        for e in errors:
            print(" - " + e, file=sys.stderr)
        return 1
    print(f"对比度全部通过（{len(results)} 项，含装饰线豁免）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
