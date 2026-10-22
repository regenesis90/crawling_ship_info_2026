from datetime import date
from pathlib import Path
import pytest
from ship_notice import Notice, NoticeClient, CrawlError, VoyageParser, Pipeline, Store, body_dates, expand_table
from bs4 import BeautifulSoup


def parse(html, title='여수-역포항로 운항 통제', published='2026-10-20'):
    return VoyageParser().parse(Notice('test', title, published, 'ctl05$open'), html)


def test_two_departures_not_arrival():
    rows, status, _ = parse('<div>2026년 10월 21일<br>여수 06:20<br>역포 08:00<br>기상악화로 인한 통제</div>')
    assert status == '추출성공'
    assert [(r['출발지명'], r['도착지명'], r['출발시각'], r['도착시각']) for r in rows] == [('여수', '역포', '06:20', None), ('역포', '여수', '08:00', None)]
    assert all(r['운항일자'] == '2026-10-21' for r in rows)


def test_table_six_trips():
    html = '<p>2026년 10월 21일</p><table><tr><th>출발지</th><th>1항차</th><th>2항차</th><th>3항차</th></tr><tr><td>여수</td><td>06:20</td><td>09:00</td><td>12:00</td></tr><tr><td>역포</td><td>08:00</td><td>10:30</td><td>13:30</td></tr></table>'
    rows, status, _ = parse(html, '여수-역포항로 시간표 변경')
    assert len(rows) == 6 and status == '추출성공'


def test_date_rows_and_arrival_columns():
    html = '<table><tr><th>날짜</th><th>출발지</th><th>출발시각</th><th>도착시각</th></tr><tr><td>10/21</td><td>여수</td><td>06:20</td><td>07:40</td></tr><tr><td>10/22</td><td>역포</td><td>08:00</td><td>09:20</td></tr></table>'
    rows, _, _ = parse(html)
    assert [(r['운항일자'], r['도착시각']) for r in rows] == [('2026-10-21', '07:40'), ('2026-10-22', '09:20')]
    assert all(r['날짜추론'] for r in rows)


def test_transposed_table():
    rows, _, _ = parse('<p>10월 21일</p><table><tr><th>항차</th><th>여수 출발</th><th>역포 출발</th></tr><tr><td>1</td><td>06:20</td><td>08:00</td></tr></table>')
    assert len(rows) == 2


def test_before_after_columns():
    rows, _, _ = parse('<p>10월 21일</p><table><tr><th>출발지</th><th>변경 전</th><th>변경 후</th></tr><tr><td>여수</td><td>06:20</td><td>07:00</td></tr></table>', '여수-역포항로 시간표 변경')
    assert [r['출발시각'] for r in rows] == ['07:00']


def test_added_only():
    rows, _, _ = parse('<p>10월 21일</p><table><tr><th>출발지</th><th>기존 정상운항</th><th>추가운항</th></tr><tr><td>여수</td><td>06:20</td><td>12:00</td></tr></table>', '여수-역포항로 증회')
    assert [r['출발시각'] for r in rows] == ['12:00']


def test_partial_afternoon():
    rows, _, _ = parse('<p>10월 21일 오후항차 통제</p><p>여수 06:20, 14:00</p><p>역포 08:00, 16:00</p>')
    assert {r['출발시각'] for r in rows} == {'14:00', '16:00'}


def test_full_control_held():
    rows, state, _ = parse('<p>10월 21일 전면 통제 여수 06:20</p>')
    assert rows == [] and state == '전면통제_보류'


def test_no_date_does_not_use_publication():
    rows, state, _ = parse('<p>여수 06:20</p>')
    assert rows == [] and state == '시간표해석실패'


def test_title_times_never_become_rows():
    assert parse('<p>홈페이지 이용 안내</p>', '여수-역포항로 10월 21일 여수 06:20 통제')[0] == []


@pytest.mark.parametrize('text,expected,inferred', [
    ('10월 21일', ['2026-10-21'], True),
    ('2026년 10월 21일', ['2026-10-21'], False),
    ('10/21', ['2026-10-21'], True),
    ('10월 21일~10월 23일', ['2026-10-21','2026-10-22','2026-10-23'], True),
    ('내일', ['2026-10-21'], True),
    ('금일', ['2026-10-20'], True),
])
def test_dates(text, expected, inferred):
    dates, flag, _ = body_dates(text, date(2026,10,20))
    assert [d.isoformat() for d in dates] == expected and flag == inferred


def test_new_year():
    assert body_dates('1월 1일', date(2026,12,31))[0] == [date(2027,1,1)]


def test_range_expands_each_voyage():
    rows, _, _ = parse('<p>10월 21일~10월 23일</p><p>여수 06:20</p>')
    assert len(rows) == 3


def test_image_not_empty_success():
    assert parse('<div><img src="timetable.png"></div>')[1] == '시간표해석실패'


def test_unknown_route_is_not_guessed():
    assert parse('<p>10월 21일 여수 06:20</p>', '운항 통제')[0] == []


def test_rowspan_grid():
    soup = BeautifulSoup('<table><tr><th rowspan="2">여수</th><td>06:20</td></tr><tr><td>09:00</td></tr></table>', 'lxml')
    assert expand_table(soup.table) == [['여수','06:20'],['여수','09:00']]


def listing_html(target='ctl05$grid$ctl02$link'):
    return f'''<form action="View.aspx?CompanyID=9214&MenuCode=24"><input type="hidden" name="__VIEWSTATE" value="fresh"><input type="hidden" name="__EVENTVALIDATION" value="valid"><table id="ctl05_p_dgMyJournal"><tr><td>100</td><td><a href="javascript:__doPostBack('{target}','')">여수-역포항로 통제</a></td><td>2026-10-20</td></tr></table></form>'''


def test_stable_id_independent_of_row_position():
    a = NoticeClient.list_from_html(listing_html())[0]
    b = NoticeClient.list_from_html(listing_html('ctl05$grid$ctl03$link'))[0]
    assert a.id == b.id and a.target != b.target


def test_listing_returned_as_detail_is_rejected():
    n = NoticeClient.list_from_html(listing_html())[0]
    with pytest.raises(CrawlError, match='목록 반환'):
        NoticeClient.detail_from_html(listing_html(), n)


def test_labelled_detail_preserves_table():
    n = NoticeClient.list_from_html(listing_html())[0]
    html = '<table><tr><td>내용</td><td><p>10월 21일</p><table><tr><td>여수</td><td>06:20</td></tr></table></td></tr></table>'
    assert '<table>' in NoticeClient.detail_from_html(html, n)


def test_post_uses_current_hidden_fields_and_row(tmp_path):
    client = NoticeClient(debug_dir=tmp_path, interval=0)
    old = NoticeClient.list_from_html(listing_html('old'))[0]
    calls = []
    class Response:
        url = 'http://company.haewoon.co.kr/CompanySite/View.aspx?CompanyID=9214&MenuCode=24'
        def __init__(self, text): self.text = text
    def request(method, url, **kw):
        calls.append((method, kw))
        return Response(listing_html('current') if method == 'GET' else '<h1>여수-역포항로 통제</h1><div id="ctl05_lblContent">10월 21일 여수 06:20</div>')
    client.request = request
    body, _ = client.detail(old)
    payload = calls[1][1]['data']
    assert payload['__VIEWSTATE'] == 'fresh' and payload['__EVENTVALIDATION'] == 'valid'
    assert payload['__EVENTTARGET'] == 'current' and '06:20' in body


def test_persistence_no_duplicate_and_failed_body_retry(tmp_path):
    class FakeClient:
        calls = 0
        def listing(self): return [Notice('a', '여수-역포항로 통제', '2026-10-20', 'a')]
        def detail(self, notice):
            self.calls += 1
            return '<p>10월 21일</p><p>여수 06:20</p>', ''
    client = FakeClient()
    p = Pipeline(client, tmp_path)
    first = p.run_once()
    assert len(first[0]) == 1 and len(first[2]) == 1
    Pipeline(client, tmp_path).run_once()
    assert client.calls == 1
    assert len(Store(tmp_path).voyages()) == 1
    import pandas as pd
    assert len(pd.read_csv(tmp_path/'voyages.csv')) == 1


def test_list_failure_propagates_without_fabricated_results(tmp_path):
    class BlockedClient:
        def listing(self): raise CrawlError('403 Domain forbidden')
    p = Pipeline(BlockedClient(), tmp_path)
    with pytest.raises(CrawlError): p.run_once()
    assert p.store.voyages().empty


def test_monitor_explicit_and_stoppable(tmp_path):
    p = Pipeline(directory=tmp_path)
    assert p.thread is None
    p.start()
    assert p.thread.is_alive()
    assert p.stop()


def test_explicit_arrival_not_second_departure():
    rows, _, _ = parse('<p>10월 21일</p><p>여수 출발 06:20 → 역포 도착 08:00</p>')
    assert len(rows) == 1 and rows[0]['도착시각'] == '08:00'


def test_changed_sections_do_not_include_old():
    rows, _, _ = parse('<p>10월 21일</p><p>변경 전</p><p>여수 06:20</p><p>변경 후</p><p>여수 07:00</p>', '여수-역포항로 시간표 변경')
    assert [r['출발시각'] for r in rows] == ['07:00']


def test_table_old_and_new_rows():
    rows, _, _ = parse('<p>10월 21일</p><table><tr><th>구분</th><th>출발지</th><th>출발시각</th></tr><tr><td>변경 전</td><td>여수</td><td>06:20</td></tr><tr><td>변경 후</td><td>여수</td><td>07:00</td></tr></table>', '여수-역포항로 시간표 변경')
    assert [r['출발시각'] for r in rows] == ['07:00']


def test_body_failure_and_parse_failure_are_distinct(tmp_path):
    class Client:
        def listing(self):
            return [Notice('a', '여수-역포항로 통제', '2026-10-20','a'), Notice('b', '여수-역포항로 통제', '2026-10-21','b')]
        def detail(self, n):
            if n.id == 'a': raise CrawlError('목록 반환')
            return '<p>여수 06:20</p>', ''
    p = Pipeline(Client(), tmp_path)
    _, audit, voyages = p.run_once()
    assert list(audit['추출상태']) == ['본문수집실패', '시간표해석실패']
    assert list(audit['상세본문 수집 성공 여부']) == [False, True]
    assert voyages.empty


def test_retry_failed_body_maximum_three_attempts(tmp_path):
    class Client:
        calls = 0
        def listing(self): return [Notice('a', '통제','2026-10-20','a')]
        def detail(self, n):
            self.calls += 1
            raise CrawlError('실패')
    c = Client()
    p = Pipeline(c, tmp_path)
    for _ in range(5): p.run_once(retry_failed=True)
    assert c.calls == 3


def test_unknown_mixed_table_is_not_silently_successful():
    rows, status, _ = parse('<p>10월 21일</p><table><tr><th>항구</th><th>시간</th></tr><tr><td>여수</td><td>06:20</td></tr><tr><td>알수없음</td><td>08:00</td></tr></table>')
    assert len(rows) == 1 and status == '부분추출'


def real_notice(id):
    import json
    data = json.loads((Path(__file__).parent/'fixtures'/'real_notices.json').read_text('utf-8'))
    entry = next(e for e in data if e['notice']['id'] == id)
    return Notice(**entry['notice']), entry['body']


def test_real_nested_body_spans_are_not_selected_as_entire_body():
    n, body = real_notice('12931292')
    result = NoticeClient.detail_from_html(body, n)
    assert '18:30' in result and '20:00' in result and '17:00' in result


def test_real_night_notice_excludes_normal_voyages():
    n, body = real_notice('12931292')
    rows, state, _ = VoyageParser().parse(n, body)
    assert state == '추출성공'
    assert [(r['출발지명'],r['출발시각']) for r in rows] == [('신기','18:30'),('여천','19:00'),('신기','19:30'),('여천','20:00')]


def test_real_holiday_table_recovers_route_from_departure_headers():
    n, body = real_notice('12871286')
    rows, state, _ = VoyageParser().parse(n, body)
    assert state == '추출성공' and len(rows) == 24
    assert {r['항로명'] for r in rows} == {'신기-여천'}
    assert {r['운항일자'] for r in rows} == {'2026-09-27'}


def test_real_three_day_table_includes_middle_day():
    n, body = real_notice('12801279')
    rows, state, _ = VoyageParser().parse(n, body)
    assert state == '추출성공' and len(rows) == 78
    from collections import Counter
    assert Counter(r['운항일자'] for r in rows) == {'2026-09-24':26,'2026-09-25':26,'2026-09-26':26}


def test_real_body_title_date_disagreement_flagged():
    n, body = real_notice('12751274')
    rows, state, why = VoyageParser().parse(n, body)
    assert state == '부분추출' and '불일치' in why
    assert len(rows) == 6 and {r['운항일자'] for r in rows} == {'2026-09-03'}


def test_malformed_tr_does_not_drop_records():
    html = '''<table id="ctl05_p_dgMyJournal"><tr><td><div style="display:none">101100</div></td><td><a href="javascript:__doPostBack('a','')">여수-역포 통제</a></td><td class="date">[2026-10-21]</td></tr><tr=""><td><div style="display:none">10099</div></td><td><a href="javascript:__doPostBack('b','')">신기-여천 통제</a></td><td class="date">[2026-10-20]</td></table>'''
    ns = NoticeClient.list_from_html(html)
    assert len(ns) == 2 and ns[1].id == '10099' and ns[1].published == '2026-10-20'


def test_date_section_inside_table_applies_to_following_rows():
    rows, state, _ = parse('<table><tr><th colspan="2">10월 21일</th></tr><tr><th>출발지</th><th>출발시각</th></tr><tr><td>여수</td><td>06:20</td></tr></table>')
    assert len(rows) == 1 and rows[0]['운항일자'] == '2026-10-21'


def test_status_column_excludes_normal_voyages():
    rows, state, _ = parse('<p>10월 21일</p><table><tr><th>출발지</th><th>출발시각</th><th>상태</th></tr><tr><td>여수</td><td>06:20</td><td>정상</td></tr><tr><td>역포</td><td>08:00</td><td>통제</td></tr></table>')
    assert [r['출발시각'] for r in rows] == ['08:00']
