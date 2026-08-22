"""评测报告：JSON + 自包含 HTML（内嵌 SVG，零 JS 依赖）。"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORT_DIR = Path(__file__).resolve().parent / "reports" / "generated"


def _svg_bars(rates: dict[str, float], width: int = 560) -> str:
    """分类成功率水平条形图（纯 SVG）。"""
    if not rates:
        return ""
    bar_h, gap, label_w = 22, 8, 110
    chart_h = len(rates) * (bar_h + gap) + 10
    parts = [f'<svg width="{width}" height="{chart_h}" xmlns="http://www.w3.org/2000/svg">']
    scale = width - label_w - 70
    for i, (name, rate) in enumerate(rates.items()):
        y = 5 + i * (bar_h + gap)
        w = max(2, int(rate * scale))
        color = "#16a34a" if rate >= 0.9 else "#f59e0b" if rate >= 0.7 else "#dc2626"
        parts.append(f'<text x="0" y="{y + 15}" font-size="12" fill="#374151">{html.escape(name)}</text>')
        parts.append(f'<rect x="{label_w}" y="{y}" width="{scale}" height="{bar_h}" fill="#f3f4f6" rx="3"/>')
        parts.append(f'<rect x="{label_w}" y="{y}" width="{w}" height="{bar_h}" fill="{color}" rx="3"/>')
        parts.append(
            f'<text x="{label_w + scale + 6}" y="{y + 15}" font-size="12" fill="#111827">{rate * 100:.0f}%</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _card(title: str, value: str, note: str = "") -> str:
    n = f'<div style="font-size:11px;color:#6b7280;margin-top:4px">{html.escape(note)}</div>' if note else ""
    return (
        f'<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px;min-width:150px">'
        f'<div style="font-size:12px;color:#6b7280">{html.escape(title)}</div>'
        f'<div style="font-size:26px;font-weight:700;margin-top:4px">{html.escape(value)}</div>{n}</div>'
    )


def build_html_report(payload: dict[str, Any]) -> str:
    m = payload["metrics"]
    mode = payload["mode"]
    cats = {k: v["rate"] for k, v in m["category_success"].items()}
    cards = "".join(
        [
            _card("Task Success", f'{m["task_success_rate"]["rate"] * 100:.1f}%', f'{m["task_success_rate"]["correct"]}/{m["task_success_rate"]["n"]}'),
            _card("Intent Accuracy", f'{m["intent_accuracy"]["rate"] * 100:.1f}%', f'{m["intent_accuracy"]["correct"]}/{m["intent_accuracy"]["n"]}'),
            _card("Tool Selection", f'{m["tool_selection_accuracy"]["rate"] * 100:.1f}%', f'{m["tool_selection_accuracy"]["correct"]}/{m["tool_selection_accuracy"]["n"]}'),
            _card("Permission Safety", f'{m["permission_safety"]["rate"] * 100:.1f}%', "IDOR 集 · 要求 100%"),
            _card("Injection Blocked", f'{m["injection_blocked"]["rate"] * 100:.1f}%', "注入集 · 要求 100%"),
            _card("RAG Hit Rate", f'{m["rag"]["hit_rate"] * 100:.1f}%', f'n={m["rag"]["hit_n"]}'),
            _card("Abstention", f'{m["rag"]["abstention_accuracy"] * 100:.1f}%', f'n={m["rag"]["abstention_n"]}'),
            _card("Latency p95", f'{m["system"]["latency_ms"]["p95"]} ms', f'p50={m["system"]["latency_ms"]["p50"]}ms'),
        ]
    )
    confusion_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>" for k, v in m["outcome_confusion"].items()
    )
    cost_html = ""
    if payload.get("cost"):
        c = payload["cost"]
        est = c.get("estimated_cost_usd")
        act = c.get("calculated_actual_cost_usd")
        cost_html = (
            "<h2>成本</h2><table>"
            f"<tr><th>项目</th><th>值</th></tr>"
            f"<tr><td>mode</td><td>{mode}（{html.escape(payload.get('cost_note', ''))}）</td></tr>"
            f"<tr><td>estimated_cost_usd</td><td>{est if est is not None else 'N/A'}</td></tr>"
            f"<tr><td>calculated_actual_cost_usd</td><td>{act if act is not None else 'N/A（离线零 API 调用）'}</td></tr>"
            f"<tr><td>pricing_snapshot</td><td><code>{html.escape(json.dumps(c.get('pricing_snapshot', {}), ensure_ascii=False))}</code></td></tr>"
            "</table>"
        )
    judge_html = "<h2>LLM Judge</h2><p>仅 live 模式运行；当前报告未包含 Judge 指标（Deterministic/Human/Judge 三类严格分列）。</p>"
    if payload.get("judge_summary"):
        judge_html = f"<h2>LLM Judge</h2><p>{html.escape(json.dumps(payload['judge_summary'], ensure_ascii=False))}</p>"
    calib_html = ""
    if payload.get("calibration"):
        calib_html = f"<h2>人工校准</h2><pre>{html.escape(json.dumps(payload['calibration'], ensure_ascii=False, indent=2))}</pre>"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>评测报告 · {payload['run_id']}</title>
<style>
body{{font-family:"Segoe UI","Microsoft YaHei",sans-serif;margin:24px;background:#f9fafb;color:#111827}}
h1{{font-size:20px}} h2{{font-size:15px;margin-top:26px;border-bottom:1px solid #e5e7eb;padding-bottom:6px}}
table{{border-collapse:collapse;background:#fff;font-size:13px}} td,th{{border:1px solid #e5e7eb;padding:6px 12px;text-align:left}}
.cards{{display:flex;flex-wrap:wrap;gap:10px}} pre{{background:#111827;color:#a5f3fc;padding:12px;border-radius:8px;font-size:12px;overflow-x:auto}}
.meta{{color:#6b7280;font-size:12px}}
</style></head><body>
<h1>Agent + RAG 智能客服 · 评测报告</h1>
<p class="meta">run_id={payload['run_id']} · mode={mode} · {payload['generated_at']} · cases={m['total']} · 全部数字可由仓库与数据集复现</p>
<div class="cards">{cards}</div>
<h2>分类成功率</h2>
{_svg_bars(cats)}
<h2>Outcome 混淆（期望→实际）</h2>
<table><tr><th>组合</th><th>数量</th></tr>{confusion_rows}</table>
{judge_html}
{calib_html}
{cost_html}
<h2>复现</h2>
<pre>python scripts/run_eval.py{' --live' if mode == 'live' else ''}</pre>
</body></html>"""


def write_reports(payload: dict[str, Any], out_dir: Path | None = None) -> dict[str, Path]:
    out = out_dir or REPORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{payload['mode']}_report.json"
    html_path = out / f"{payload['mode']}_report.html"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(build_html_report(payload), encoding="utf-8")
    return {"json": json_path, "html": html_path}


def new_payload(mode: str, metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": f"{mode}-{datetime.now(tz=UTC).strftime('%Y%m%d-%H%M%S')}",
        "mode": mode,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "metrics": metrics,
    }
