"""저장된 실제 답변을 읽기 전용 HTML 비교 뷰로 표시한다. 답변을 생성하지 않는다."""
import html
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "results/qa-comparison-2026-10-21"


def main():
    data = json.loads((OUT / "responses.json").read_text())
    assert data["metadata"]["status"] == "completed", "완료된 실행 결과가 필요합니다."
    labels = {"target": "01 · 조사 대상 확인", "cpu": "02 · 수치 조건과 근거 확인", "recovery": "03 · 한 번의 정상화와 복구 구분"}
    css = """*{box-sizing:border-box}body{margin:0;background:#edf1f6;color:#182239;font-family:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo',sans-serif}
header{background:#17233a;color:white;padding:14px 30px;display:flex;align-items:center;justify-content:space-between}
header strong{font-size:21px}nav{display:flex;gap:10px}nav a{color:#e1e8f4;text-decoration:none;padding:8px 12px;border:1px solid #61718b;border-radius:7px;font-size:14px}
main{padding:15px 30px;max-width:1600px;margin:auto}.intro{font-size:13px;color:#526079;margin-bottom:8px}
h1{font-size:25px;margin:0 0 10px}.question{padding:12px 18px;border-left:5px solid #4c58d9;background:white;font-size:18px;margin-bottom:10px}
.input{font-size:14px;background:#dfe7f0;border-radius:7px;padding:10px 18px;margin-bottom:14px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
article{background:white;border:1px solid #d5deea;border-radius:12px;overflow:hidden}.label{padding:12px 18px;font-weight:700;font-size:20px;background:#e9edf3}.after .label{background:#e0f0ec;color:#126552}
.info{padding:9px 18px;color:#59677e;font-size:13px;border-bottom:1px solid #e7ebf2}.reply{padding:14px 18px;white-space:pre-wrap;overflow-wrap:anywhere;font-family:inherit;font-size:15.5px;line-height:1.5;margin:0}
details{border-top:1px solid #e7ebf2;padding:12px 18px;color:#475870;font-size:14px}details pre{white-space:pre-wrap;font-size:14px;color:#182239}
footer{margin-top:14px;font-size:13px;color:#526079;line-height:1.7}.note{padding:10px 0 0;font-size:14px;color:#526079}
"""
    for case, label in labels.items():
        step = 2 if case == "recovery" else 1
        rows = {r["version"]: r for r in data["rows"] if r["case"] == case and r["step"] == step}
        assert all(rows[v]["status"] == "completed" for v in ("before", "after"))
        before, after = rows["before"], rows["after"]
        assert before["query"] == after["query"] and before["snapshot"] == after["snapshot"]
        s = before["snapshot"]
        if case == "target":
            input_text = "서비스명 미지정 · 원본 코드가 선택한 auth-service 조회에만 고정 관측값 제공 · 개선 후에는 조회하지 않음"
        elif case == "recovery":
            input_text = "같은 대화에서 10초 간격 관측: degraded / CPU 95% / 메모리 90% / 알림 2건 → healthy / CPU 55% / 메모리 60% / 알림 0건"
        else:
            input_text = "동일 입력: payments-service · degraded · CPU 89.5% · 메모리 55% · 알림 CPU high (threshold 80%) · 동일 CPU 런북"
        panels = []
        for version, row in rows.items():
            reply = row["reply"]
            excerpt = reply
            if case == "cpu" and version == "after":
                selected = [line for line in reply.splitlines() if
                            line.startswith("## 확인된") or line.startswith("- [M1] payments-service") or
                            line.startswith("## 추가") or line.startswith("- [M1] CPU 부하") or
                            line.startswith("## 권장") or "조회한 런북:" in line or "런북 수치 조건:" in line or
                            line.startswith("확인 범위:")]
                excerpt = "\n".join(selected)
            targets = ", ".join(c["system_name"] for c in row["tool_calls"]) or "없음"
            info = f"실제 모니터링 조회: {targets} · " + ("원문 발췌" if excerpt != reply else "답변 원문")
            name = "개선 전 · 원본 d1043ff" if version == "before" else "개선 후 · 1296a54"
            panels.append(f'<article class="{version}"><div class="label">{name}</div><div class="info">{html.escape(info)}</div>'
                          f'<pre class="reply">{html.escape(excerpt)}</pre>'
                          f'<details><summary>전체 답변 원문 보기</summary><pre>{html.escape(reply)}</pre></details></article>')
        links = "".join(f'<a href="{c}.html">{l}</a>' for c, l in labels.items())
        document = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>{label} — 실제 실행 비교</title>
<style>{css}</style><header><strong>운영장애 대응 에이전트 · 실제 실행 기록</strong><nav>{links}</nav></header>
<main><div class="intro">2026.10.21 실행 · 읽기 전용 비교 뷰 (제품 채팅 UI와 구분)</div><h1>{label}</h1>
<div class="question">질문: {html.escape(before['query'])}</div><div class="input">{html.escape(input_text)}</div>
<div class="grid">{''.join(panels)}</div>
<footer>원본·개선 그래프와 각 버전의 Ollama 연결 코드 실행 / 같은 모델 qwen3:0.6b · qwen3:8b / 모니터링 입력은 고정된 합성 자료<br>
개선 후 대상 확인·복구 판단은 LLM 없이 코드로 처리합니다. 질문별 1회 실행 사례이며 전체 정확도·운영 환경 성능을 뜻하지 않습니다.
 · <a href="responses.json">전체 실행 원문 JSON</a></footer></main></html>'''
        # 원문의 줄 끝 공백은 DOM에 보존하면서 HTML 소스의 trailing whitespace는 피한다.
        (OUT / f"{case}.html").write_text(document.replace(" \n", "&#32;\n"))
    print("HTML 비교 화면 3개 생성 완료")


if __name__ == "__main__":
    main()
