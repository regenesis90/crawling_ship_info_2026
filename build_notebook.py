"""Regenerate the standalone notebook from the tested implementation."""
from pathlib import Path
import nbformat as nbf

ROOT = Path(__file__).resolve().parent
nb = nbf.v4.new_notebook()
md = nbf.v4.new_markdown_cell
code = nbf.v4.new_code_cell
nb.cells = [
    md('''# 한림해운 여수 공지사항 → 항차별 운항정보

브라우저 자동화 없이 `requests.Session`과 ASP.NET PostBack만 사용합니다.
**목록 → 키워드 선별 → 실제 상세 본문 → 항차 해석** 순서로 실행합니다.
본문에서 확인되지 않은 도착시각은 결측값입니다. 전면통제는 본문 수집 후 보류합니다.

이 노트북은 구현 코드를 포함하므로 `ship_notice.py` 없이도 실행할 수 있습니다.
Python 3.12에서 검증했습니다. 필요한 패키지는 아래 명령으로 설치한 후 Python 커널을 선택하세요.

```bash
python -m pip install requests==2.34.2 beautifulsoup4==4.15.0 pandas==3.0.6 lxml==6.1.3 ipykernel==7.4.0
```

처음에는 첫 페이지 최대 30개를 조회합니다. 제목에 지정 키워드가 없는 글의 상세는 요청하지 않습니다.
30분 감시는 기본적으로 꺼져 있으며 마지막 부분의 별도 셀에서 시작·중지합니다.
출력에는 예시 항차를 섞지 않습니다. 실제 요청이 실패하면 실패 이유를 표시합니다.'''),
    md('''## 1. 수집·분석 구현

정규표현식은 날짜와 시각 토큰을 읽는 용도입니다. 항차는 **항로 방향, 표 헤더와 행·열,
변경 전/후 구분, 정상운항 이후 통제 구간, 문단별 출발지·적용일**을 결합하여 만듭니다.
임의의 자연어·이미지를 모두 이해하는 모델은 아닙니다. 불명확한 내용은 실패/부분추출로 남깁니다.
연도가 생략된 날짜는 게시일에 가장 가까운 연도를 사용하고 추론 여부를 기록합니다.'''),
]
source = (ROOT/'ship_notice.py').read_text(encoding='utf-8')
# Keep readable sections while retaining a standalone runnable implementation.
markers = ['class NoticeClient:', 'def expand_table(', 'class VoyageParser:', 'class Store:', 'class Pipeline:']
positions = [0] + [source.index(m) for m in markers] + [len(source)]
for i in range(len(positions)-1):
    nb.cells.append(code(source[positions[i]:positions[i+1]].strip()))
nb.cells += [
    md('''## 2. 저장 위치 및 최초 수집 — 감시는 시작하지 않음

`data/notices.sqlite3`가 처리 이력과 항차의 원본입니다. 재실행해도 성공한 글은 다시 수집하지 않습니다.
본문수집실패는 `retry_failed=True`로 최대 3회까지 시도합니다. 파서 개선 후 기존 본문을 다시 분석하려면
아래 재분석 셀을 사용하세요. SQLite 파일을 삭제하면 처리 이력도 삭제되므로 주의하세요.
원본 전체 응답 HTML은 `data/debug/`에 보존합니다. 요청 간격은 기본 1.5초이며 타임아웃과 GET 재시도를 사용합니다.

HTTP 주소는 사이트의 원래 주소입니다. HTTPS는 다른 응답을 줄 수 있습니다.
Chrome User-Agent를 사용하지만 브라우저를 실행하지 않습니다.
환경 프록시가 `Domain forbidden`으로 차단하면 헤더만으로 해결되지 않을 수 있으므로 도메인 허용 설정을 확인하세요.'''),
    code('''DATA_DIR = Path("data")
# 같은 커널에서 셀을 재실행할 때 이전 감시를 먼저 중단합니다.
if "pipeline" in globals():
    if not pipeline.stop():
        raise RuntimeError("이전 수집 요청 종료 후 이 셀을 다시 실행하세요.")
pipeline = Pipeline(directory=DATA_DIR)
run_error = None
try:
    listing_df, detail_df, voyages_df = pipeline.run_once(retry_failed=True)
    listing_df.to_csv(DATA_DIR / "listing.csv", index=False, encoding="utf-8-sig")
except (requests.RequestException, CrawlError, ValueError) as exc:
    run_error = f"{type(exc).__name__}: {exc}"
    listing_df = pd.DataFrame(columns=["게시글ID", "제목", "게시일", "키워드 해당 여부"])
    detail_df = pipeline.store.audit()
    voyages_df = pipeline.store.voyages()
    print("이번 목록 수집 실패:", run_error)
    print("상세/항차 출력에 기존 이력이 있을 수 있습니다. 아래 요약은 이번 목록 대상만 집계합니다.")'''),
    md('## 3. 첫 번째 DataFrame — 실제 첫 페이지 목록'),
    code('''from IPython.display import display
pd.set_option("display.max_rows", 100)
pd.set_option("display.max_colwidth", 120)
display(listing_df)
if not run_error and len(listing_df) != 30:
    print(f"주의: 서버 목록에서 {len(listing_df)}개를 확인했습니다. 30개로 채우기 위한 가상 행을 만들지 않습니다.")'''),
    md('## 4. 두 번째 DataFrame — 상세 본문 수집 및 분석 상태'),
    code('''current_ids = set(listing_df["게시글ID"])
current_details = detail_df[detail_df["게시글ID"].isin(current_ids)].copy()
display(current_details)
# 키워드가 없는 글은 제목만 확인하며 상세 수집 대상이 아닙니다.'''),
    md('## 5. 세 번째 DataFrame — 항차 단위 운항정보'),
    code('''current_voyages = voyages_df[voyages_df["게시글ID"].isin(current_ids)].copy()
display(current_voyages[CORE + EXTRA])
print("누적 CSV:", str((DATA_DIR / "voyages.csv").resolve()))'''),
    md('## 6. 실패·보류 목록 및 이번 첫 페이지 검증 요약'),
    code('''failure_states = ["본문수집실패", "시간표해석실패", "부분추출"]
failures_df = current_details[current_details["추출상태"].isin(failure_states)]
display(failures_df[["게시글ID", "제목", "추출상태", "실패단계", "실패이유"]])
display(current_details[current_details["추출상태"] == "전면통제_보류"])
summary = {
    "검증시각": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
    "목록수집오류": run_error,
    "실제목록게시글수": len(listing_df),
    "키워드대상수": int(listing_df["키워드 해당 여부"].sum()),
    "상세본문수집성공수": int(current_details["상세본문 수집 성공 여부"].sum()),
    "항차추출수": len(current_voyages),
    "상태별게시글수": {str(k): int(v) for k, v in current_details["추출상태"].value_counts().items()},
    "실패목록": failures_df[["게시글ID", "제목", "추출상태", "실패단계", "실패이유"]].to_dict("records"),
}
(DATA_DIR / "validation_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
display(pd.DataFrame([{k:v for k,v in summary.items() if k not in ("실패목록", "상태별게시글수")}]))
print(json.dumps(summary["상태별게시글수"], ensure_ascii=False))'''),
    md('''## 7. 선택: 저장된 본문만 재분석

네트워크 재수집 없이 로직 개선 후 분석 결과를 갱신합니다. 기본 실행에는 포함하지 않습니다.'''),
    code('''REANALYZE_SAVED = False
if REANALYZE_SAVED:
    with pipeline.lock:
        with pipeline.store.connect() as db:
            saved = db.execute("SELECT payload,body FROM notices WHERE body IS NOT NULL").fetchall()
        for payload, body in saved:
            notice = Notice(**json.loads(payload))
            rows, state, why = pipeline.parser.parse(notice, body)
            pipeline.store.save(notice, body, state, why, rows)
        pipeline.store.export()
    display(pipeline.store.audit())
    display(pipeline.store.voyages())'''),
    md('''## 8. 선택: 30분 감시 시작 — 명시적으로 켤 때만 실행

아래 값을 `True`로 변경하고 이 셀을 실행해야 시작됩니다. 첫 추가 확인은 30분 뒤입니다.
노트북 커널이 살아 있는 동안만 동작합니다. 재시작 후 다시 시작해야 하며,
첫 페이지 30개를 넘는 게시글이 감시 사이에 새로 올라오면 그 이전 글은 놓칠 수 있습니다.
이미 처리한 게시글의 수정 사항은 자동 재수집하지 않습니다.'''),
    code('''START_MONITOR = False
if START_MONITOR:
    pipeline.start(seconds=30 * 60)
    print("30분 감시 시작. 중지는 다음 셀에서 수행합니다.")
else:
    print("자동 감시는 시작하지 않았습니다.")'''),
    md('## 9. 감시 상태 확인 및 중지'),
    code('''print("감시 중:", bool(pipeline.thread and pipeline.thread.is_alive()))
print("마지막 감시 오류:", pipeline.last_error)
# 감시 중 최신 누적 결과:
# display(pipeline.store.voyages())
# 즉시 중지하려면 아래 주석을 해제합니다. 진행 중인 HTTP 요청은 타임아웃까지 종료를 기다릴 수 있습니다.
# print("중지 완료:", pipeline.stop())'''),
    md('''## 해석 한계와 검토 방법

* 전면통제는 의도적으로 항차를 생성하지 않습니다.
* 이미지 시간표, 복수 항로, 적용일·출발지 불명확 사례는 수동 검토 대상입니다.
* 사유는 본문에 근거가 있을 때만 기록합니다. 도착시각과 정규 시간표는 임의 보완하지 않습니다.
* 동일한 ID는 재처리하지 않습니다. 사이트의 숨김 키가 없을 때만 제목+게시일 지문을 사용하며, 충돌은 오류로 처리합니다.
* 수집 실패와 해석 실패는 별개입니다. `audit.csv`의 단계·원인과 `debug/` HTML로 원문을 확인하세요.
* 이 코드는 확인한 게시판 형식과 명시적인 표/문장 관계를 지원합니다. 모든 자연어 형식의 완전한 해석을 보장하지 않습니다.
* 실제 크롤링 요약은 6번 셀에서 생성합니다. 회귀 테스트용 자료는 `tests/`에만 있으며 실제 출력에 사용하지 않습니다.'''),
]
nb.metadata = {'kernelspec': {'display_name':'Python 3', 'language':'python','name':'python3'}, 'language_info':{'name':'python','version':'3.12.14'}}
nbf.write(nb, ROOT/'한림해운_운항정보.ipynb')
print(ROOT/'한림해운_운항정보.ipynb')
