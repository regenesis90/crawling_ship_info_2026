"""Requests-only Web Forms crawler and conservative, structure-aware notice parser.

No arrival time, voyage date or direction is synthesized without evidence.
Unsupported/ambiguous formats stay visible in the per-notice audit frame.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URL = 'http://company.haewoon.co.kr/CompanySite/View.aspx?CompanyID=9214&MenuCode=24'
KEYWORDS = ('통제', '결항', '운항중단', '증회', '추가운항', '임시운항', '시간표', '시간변경', '운항시간변경')
CORE = ['운항일자', '항로명', '출발지명', '도착지명', '출발시각', '도착시각', '유형', '사유']
EXTRA = ['게시글ID', '게시글제목', '공지게시일', '추출상태', '원문URL', '추출근거', '날짜추론', '날짜근거']
DATE_RE = re.compile(r'(?:(?P<y>20\d{2})\s*[년./-]\s*)?(?P<m>\d{1,2})\s*[월./-]\s*(?P<d>\d{1,2})\s*일?')
TIME_RE = re.compile(r'(?<![\d:])(?P<h>[01]?\d|2[0-3])\s*:\s*(?P<m>[0-5]\d)(?!\d)|(?P<kh>[01]?\d|2[0-3])\s*시\s*(?P<km>[0-5]?\d)?\s*분?')
ROUTE_RE = re.compile(r'([가-힣A-Za-z]+?)\s*[-~↔–→]\s*([가-힣A-Za-z]+?)(?:항로|구간|\s|$|[()])')
POST_RE = re.compile(r"__doPostBack\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]*)['\"]\s*\)")


def compact(s):
    return re.sub(r'\s+', '', str(s))


def clean(s):
    return re.sub(r'[\xa0\t ]+', ' ', str(s)).strip()


def keyword_hit(title):
    return any(k in compact(title) for k in KEYWORDS)


def clocks(text):
    out = []
    for m in TIME_RE.finditer(text):
        h = int(m['h'] or m['kh'])
        minute = int(m['m'] or m['km'] or 0)
        # 오전/오후 applies to the closest time, not every time in a paragraph.
        prefix = text[max(0, m.start()-5):m.start()]
        if '오후' in prefix and h < 12:
            h += 12
        if '오전' in prefix and h == 12:
            h = 0
        out.append((f'{h:02}:{minute:02}', m.start(), m.end()))
    return out


def body_dates(text, published):
    """Return dates, inference flag, evidence. No publication-date fallback.

    Missing years choose the closest year to the publication date (New Year aware).
    Relative dates are relative to publication, never the crawler's current date.
    """
    found = []
    for m in DATE_RE.finditer(text):
        try:
            month, day = int(m['m']), int(m['d'])
            if m['y']:
                value = date(int(m['y']), month, day)
            elif published:
                candidates = []
                for year in (published.year-1, published.year, published.year+1):
                    try:
                        candidates.append(date(year, month, day))
                    except ValueError:
                        pass
                value = min(candidates, key=lambda d: abs((d-published).days))
            else:
                continue
            found.append((m, value, not bool(m['y'])))
        except ValueError:
            continue
    between = text[found[0][0].end():found[1][0].start()] if len(found) == 2 else ''
    between = re.sub(r'[월화수목금토일]요일|\([월화수목금토일]\)', '', between)
    if len(found) == 2 and re.fullmatch(r'\s*(?:~|～|부터|–|-)\s*', between):
        a, b = found
        end = b[1]
        if end < a[1] and not b[0]['y']:
            try:
                end = end.replace(year=a[1].year+1)
            except ValueError:
                return [], True, '날짜 범위 오류'
        days = (end-a[1]).days
        if 0 <= days <= 366:
            return [a[1]+timedelta(days=i) for i in range(days+1)], a[2] or b[2], text.strip()
        return [], True, '날짜 범위가 너무 길거나 역전됨'
    if found:
        return list(dict.fromkeys(x[1] for x in found)), any(x[2] for x in found), ' / '.join(x[0][0] for x in found)
    if published:
        rel = re.search(r'금일|오늘|내일|익일|모레', text)
        if rel:
            delta = {'금일': 0, '오늘': 0, '내일': 1, '익일': 1, '모레': 2}[rel[0]]
            return [published+timedelta(days=delta)], True, rel[0]+' (게시일 기준)'
    return [], False, ''


@dataclass
class Notice:
    id: str
    title: str
    published: str | None
    target: str
    argument: str = ''
    href: str = ''


class CrawlError(RuntimeError):
    pass


class NoticeClient:
    def __init__(self, url=URL, debug_dir='data/debug', interval=1.5, timeout=(10, 30)):
        self.url = url
        self.debug_dir = Path(debug_dir)
        self.interval = interval
        self.timeout = timeout
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36', 'Accept-Language': 'ko,en;q=0.5'})
        # POST is deliberately not blindly retried: ViewState can be stateful.
        retry = Retry(total=2, backoff_factor=1, status_forcelist=[429, 502, 503, 504], allowed_methods=['GET'], respect_retry_after_header=True)
        self.session.mount('http://', HTTPAdapter(max_retries=retry))
        self.session.mount('https://', HTTPAdapter(max_retries=retry))

    def request(self, method, url, **kwargs):
        time.sleep(max(0, self.interval-(time.monotonic()-self.last_request)))
        try:
            r = self.session.request(method, url, timeout=self.timeout, **kwargs)
            if r.status_code >= 400:
                self.save_debug(f'http-{r.status_code}-{method}', r.text)
            r.raise_for_status()
            r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == 'iso-8859-1' else r.encoding
            if re.search(r'Server Error in|Runtime Error|Validation of viewstate MAC failed|Invalid postback', r.text, re.I):
                self.save_debug('aspnet-server-error', r.text)
                raise CrawlError('ASP.NET 서버 오류 응답')
            return r
        finally:
            self.last_request = time.monotonic()

    def save_debug(self, key, html):
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        safe = hashlib.sha256(str(key).encode()).hexdigest()[:16]
        path = self.debug_dir / f'{safe}.html'
        path.write_text(html, encoding='utf-8')
        return str(path)

    @staticmethod
    def list_from_html(html):
        soup = BeautifulSoup(html, 'lxml')
        table = soup.find(id=re.compile(r'(?:^|_)dgMyJournal$'))
        if not table:
            raise CrawlError('공지사항 목록 테이블 dgMyJournal 없음: 차단/레이아웃 변경 확인 필요')
        notices = []
        for a in table.find_all('a'):
            js = ' '.join([a.get('href', ''), a.get('onclick', '')])
            pb = POST_RE.search(js)
            title = clean(a.get_text(' ', strip=True))
            if not pb or not title or pb[2].lower().startswith('page$') or re.fullmatch(r'[\d<>»«]+|다음|이전|처음|마지막', title):
                continue
            # The live page contains invalid <tr=""> tags. Traverse title-cell
            # siblings instead of assuming every record is a valid <tr>.
            td = a.find_parent('td')
            if td is None:
                continue
            siblings = td.find_next_siblings('td')
            date_cell = next((c for c in siblings if 'date' in c.get('class', [])), None)
            text = date_cell.get_text(' ', strip=True) if date_cell else ' '.join(c.get_text(' ', strip=True) for c in siblings)
            dm = re.search(r'20\d{2}[./-]\d{1,2}[./-]\d{1,2}', text)
            pub = None
            if dm:
                y, m, d = map(int, re.split(r'[./-]', dm[0]))
                pub = date(y, m, d).isoformat()
            record = re.search(r'(?:journalid|boardid|articleid|idx|seq)\s*[=:]\s*[\'\"]?(\d+)', js, re.I)
            number_cell = td.find_previous_sibling('td')
            hidden_key = number_cell.find('div', style=re.compile(r'display\s*:\s*none')) if number_cell else None
            if record:
                identity = record[1]
            elif hidden_key and re.fullmatch(r'\d+', hidden_key.get_text(strip=True)):
                identity = hidden_key.get_text(strip=True)  # opaque site key, not visible rowNo
            else:
                identity = hashlib.sha256((title+'|'+str(pub)).encode()).hexdigest()[:24]
            if any(n.id == identity for n in notices):
                raise CrawlError('게시글 식별자 중복: 안전한 중복 검사 불가')
            notices.append(Notice(identity, title, pub, pb[1], pb[2], a.get('href', '')))
        return notices[:30]

    def listing(self):
        r = self.request('GET', self.url)
        try:
            notices = self.list_from_html(r.text)
            if not notices:
                raise CrawlError('목록 게시글 0개: 정상 빈 게시판인지 확인 필요')
            return notices
        except Exception:
            self.save_debug('list-failed', r.text)
            raise

    @staticmethod
    def detail_from_html(html, notice):
        soup = BeautifulSoup(html, 'lxml')
        # First use explicit body controls, then label/value structure; never take
        # the entire form/page as a body because that silently parses the listing.
        explicit = soup.find_all(id=re.compile(r'(?:lbl|lit|txt)_?(?:content|contents|body)$|(?:div|td|pnl).*(?:journaltext|journalview|noticebody)$', re.I))
        # Actual posts embed copied markup with duplicate lblContent IDs. Take
        # the outer content control, not a tiny nested span containing one date.
        explicit = [c for c in explicit if not any(p in explicit for p in c.parents)]
        candidates = list(explicit)
        for cell in soup.find_all(['td', 'th', 'label']):
            if compact(cell.get_text(' ', strip=True)).rstrip(':') in ('내용', '본문', '공지내용'):
                sibling = cell.find_next_sibling(['td', 'div'])
                if sibling:
                    candidates.append(sibling)
        # Some Web Forms templates have title + author/date rows and an unlabelled
        # colspan body row. Scope to the smallest title-containing table.
        title_nodes = soup.find_all(string=lambda s: bool(s) and compact(s) == compact(notice.title))
        for node in title_nodes:
            for table in node.parents:
                if not isinstance(table, Tag) or table.name != 'table':
                    continue
                if table.find(id=re.compile('dgMyJournal')):
                    continue
                for td in table.find_all('td'):
                    if int(td.get('colspan', '1')) >= 2 and td.find(['p', 'div', 'table', 'br', 'img']) and not td.find('a', href=re.compile('__doPostBack')):
                        candidates.append(td)
                break
        for body in explicit + sorted([c for c in candidates if c not in explicit], key=lambda c: len(str(c))):
            if body.find(id=re.compile('dgMyJournal')) or body.get('id', '').endswith('dgMyJournal'):
                continue
            text = body.get_text('\n', strip=True)
            if text and compact(text) != compact(notice.title) and not body.find('input', attrs={'name': '__VIEWSTATE'}):
                return str(body)
            if body.find('img'):
                return str(body)  # image-only notices are reported, never guessed
        if soup.find(id=re.compile('dgMyJournal')):
            raise CrawlError('POST 후 목록 반환: EVENTTARGET/ARGUMENT 또는 ViewState 불일치')
        raise CrawlError('본문 식별 실패: 명시적 본문 컨트롤/내용 셀 없음; 저장 HTML 검토 필요')

    def detail(self, notice):
        # Fresh list for each post: rotating hidden fields and row positions matter.
        r = self.request('GET', self.url)
        current = self.list_from_html(r.text)
        selected = next((n for n in current if n.id == notice.id), None)
        if not selected:
            raise CrawlError('게시글이 첫 페이지에서 이동/삭제됨; 다른 행의 PostBack 사용을 중단함')
        soup = BeautifulSoup(r.text, 'lxml')
        form = soup.find('form')
        if not form:
            raise CrawlError('ASP.NET form 없음')
        payload = {i['name']: i.get('value', '') for i in form.find_all('input', type='hidden') if i.get('name')}
        if '__VIEWSTATE' not in payload:
            raise CrawlError('__VIEWSTATE 없음')
        payload.update({'__EVENTTARGET': selected.target, '__EVENTARGUMENT': selected.argument})
        action = urljoin(r.url, form.get('action') or r.url)
        if urlparse(action).netloc != urlparse(self.url).netloc:
            raise CrawlError('예상하지 않은 외부 form action')
        response = self.request('POST', action, data=payload, headers={'Referer': r.url})
        try:
            title_prefix = compact(re.sub(r'\s*(?:\.\.\.|…)\s*$', '', selected.title))
            if title_prefix not in compact(BeautifulSoup(response.text, 'lxml').get_text(' ', strip=True)):
                raise CrawlError('상세 응답에 요청 게시글 제목 없음: 다른 글/인증/오류 페이지 가능')
            html = self.detail_from_html(response.text, selected)
            # Reject a different title when an explicit detail title is available.
            page = BeautifulSoup(response.text, 'lxml')
            heading = page.find(id=re.compile(r'(?:lbl|txt|lit).*(?:subject|title)$', re.I))
            if heading and not compact(heading.get_text()).startswith(title_prefix):
                raise CrawlError('요청한 게시글과 상세 제목 불일치')
            return html, self.save_debug(selected.id, response.text)
        except Exception:
            self.save_debug(selected.id, response.text)
            raise


def expand_table(table):
    """Expand rowspan/colspan into a rectangular grid without losing headers."""
    grid, carried = [], {}
    for row in table.find_all('tr'):
        if row.find_parent('table') is not table:
            continue
        values, col = [], 0
        def fill():
            nonlocal col
            while col in carried:
                value, remaining = carried.pop(col)
                values.append(value)
                if remaining > 1:
                    carried[col] = (value, remaining-1)
                col += 1
        for cell in row.find_all(['td', 'th'], recursive=False):
            fill()
            value = cell.get_text(' ', strip=True)
            span = min(int(cell.get('colspan', 1)), 100)
            down = min(int(cell.get('rowspan', 1)), 400)
            for _ in range(span):
                values.append(value)
                if down > 1:
                    carried[col] = (value, down-1)
                col += 1
        fill()
        grid.append(values)
    width = max(map(len, grid), default=0)
    return [row+['']*(width-len(row)) for row in grid]


def notice_type(text):
    if re.search(r'통제|결항|운항\s*중단', text):
        return '통제'
    if re.search(r'증회|추가\s*운항|임시\s*운항', text):
        return '증회'
    if re.search(r'시간표|시간\s*변경|운항시간\s*변경', text):
        return '시간표변경'
    return None


def reason_from(text):
    reasons = re.findall(r'기상\s*악화|풍랑|강풍|해상\s*기상|선박\s*정비|선박\s*고장|접안시설\s*공사', text)
    if reasons:
        return ', '.join(dict.fromkeys(compact(s) for s in reasons))
    m = re.search(r'([^\n.!]{2,40}?)(?:으로|로)\s*인한', text)
    return m[1].strip() if m else None


class VoyageParser:
    """Interpret document scopes + table axes. Ambiguity is an audit result.

    This is intentionally conservative: image/OCR, unnamed routes, and unrestricted
    natural language require review instead of invented output. Regex tokenizes
    dates/times; table headers, local scopes and labels determine their meaning.
    """
    def parse(self, notice, body_html):
        soup = BeautifulSoup(body_html, 'lxml')
        for t in soup(['script', 'style']):
            t.decompose()
        text = soup.get_text('\n', strip=True)
        combined = notice.title+'\n'+text
        if re.search(r'전면\s*통제', combined):
            return [], '전면통제_보류', '본문 수집 완료; 전면통제 항차 자동 생성 보류'
        if not text and soup.find('img'):
            return [], '시간표해석실패', '이미지 본문: OCR/수동 검토 필요'
        published = date.fromisoformat(notice.published) if notice.published else None
        title_routes = list(dict.fromkeys(ROUTE_RE.findall(notice.title)))
        routes = title_routes or list(dict.fromkeys(ROUTE_RE.findall(text)))
        if not routes:
            # No route in the holiday notice title: recover endpoints only from
            # explicit departure-axis headers, never from arbitrary place names.
            axis_ports = []
            for table in soup.find_all('table'):
                for row in expand_table(table):
                    if any(clocks(v) for v in row):
                        break
                    for cell in row:
                        m = re.fullmatch(r'([가-힣]+)\s*(?:출항|출발)(?:\([^)]*\))?', compact(cell))
                        if m and m[1] not in axis_ports:
                            axis_ports.append(m[1])
            if len(axis_ports) == 2:
                routes = [tuple(axis_ports)]
        if len(routes) != 1:
            return [], '시간표해석실패' if clocks(text) else '운항정보없음', '항로 불명확/복수 항로: 방향을 추정하지 않음'
        ports = routes[0]
        route = '-'.join(ports)
        kind = notice_type(notice.title) or notice_type(text)
        if not kind:
            return [], '운항정보없음', '본문의 공지 유형 확인 불가'
        # Global dates exclude table row dates: those must apply only to their row.
        outside = BeautifulSoup(body_html, 'lxml')
        for t in outside.find_all('table'):
            t.decompose()
        prose = outside.get_text('\n', strip=True)
        global_dates = body_dates(prose, published)
        # Several unrelated dates in prose are ambiguous; don't cross-product them.
        ambiguous_dates = len(global_dates[0]) > 1 and not re.search(r'~|～|부터|–|동일|각각|및|,|·', prose)
        rows, issues = [], []
        if soup.find('img'):
            issues.append('이미지 포함: 이미지 내 시간표는 OCR/수동 확인 필요')
        title_dates = body_dates(notice.title, published)[0]
        if title_dates and global_dates[0] and set(title_dates) != set(global_dates[0]):
            issues.append('제목과 본문의 적용일 불일치: 본문 날짜 사용, 원문 확인 필요')
        all_reason = reason_from(text)

        def port_in(s):
            found = [p for p in ports if re.search(re.escape(p)+r'(?:항)?(?:출발|발)?', compact(s))]
            return found[0] if len(found) == 1 else None

        def emit(origin, depart, evidence, local_dates=None, arrival=None, destination=None, local_kind=None):
            dates, inferred, date_evidence = local_dates or global_dates
            if not origin or not dates or (ambiguous_dates and local_dates is None):
                issues.append('출발지/적용일 불명확: '+evidence)
                return
            dest = destination or next((p for p in ports if p != origin), None)
            # Explicit partial-day scope filters candidate timetable cells.
            if kind == '통제':
                if re.search(r'오후\s*항차', text) and int(depart[:2]) < 12:
                    return
                if re.search(r'오전\s*항차', text) and int(depart[:2]) >= 12:
                    return
                if re.search(r'야간|일부\s*항차', text) and '통제' not in evidence and not re.search(r'통제\s*(?:대상|시각|시간표)', text):
                    issues.append('일부/야간 대상 명시 부족: '+evidence)
                    return
            for day in dates:
                rows.append(dict(zip(CORE, [day.isoformat(), route, origin, dest, depart, arrival, local_kind or kind, all_reason])) | {
                    '게시글ID': notice.id, '게시글제목': notice.title, '공지게시일': notice.published,
                    '추출상태': '추출성공', '원문URL': URL, '추출근거': evidence,
                    '날짜추론': inferred, '날짜근거': date_evidence})

        for table in soup.find_all('table'):
            if table.find_parent('table'):
                continue
            grid = expand_table(table)
            if not grid:
                continue
            headers = {}
            section = ''
            table_dates = None
            for ri, row in enumerate(grid):
                joined = ' | '.join(row)
                tokens = clocks(joined)
                if not tokens:
                    section_dates = body_dates(joined, published)
                    if section_dates[0] and len(set(v for v in row if v)) == 1:
                        table_dates = section_dates
                    # Preserve hierarchical header rows and section labels.
                    if len(set(v for v in row if v)) == 1 and re.search(r'변경\s*전|기존|정상|변경\s*후|증회|추가|통제\s*대상', joined):
                        section = joined
                    for ci, value in enumerate(row):
                        if value:
                            headers[ci] = (headers.get(ci, '')+' '+value).strip()
                    continue
                if re.search(r'변경\s*전|기존|정상', row[0]+' '+section) and not re.search(r'변경\s*후|증회|추가', row[0]+' '+section):
                    continue
                local_dates = body_dates(joined, published)
                local_dates = local_dates if local_dates[0] else table_dates
                status_col = next((i for i, h in headers.items() if re.search(r'상태|운항여부|구분', compact(h))), None)
                if status_col is not None and status_col < len(row):
                    row_status = row[status_col]
                    if kind == '통제' and re.search(r'정상|운항가능', row_status):
                        continue
                origin_col = next((i for i, h in headers.items() if re.search(r'출발지|출발항|출항지', h)), None)
                arr_col = next((i for i, h in headers.items() if re.search(r'도착시각|도착시간|입항시간', h)), None)
                # Explicit per-voyage row: date / origin / departure / arrival.
                origin = port_in(row[origin_col]) if origin_col is not None and origin_col < len(row) else port_in(row[0])
                if origin:
                    for ci, value in enumerate(row):
                        h = headers.get(ci, '')
                        if ci == arr_col or re.search(r'도착|변경\s*전|기존', h):
                            continue
                        if kind == '증회' and re.search(r'기존|정상', h) and not re.search(r'증회|추가', h):
                            continue
                        arrival = None
                        if arr_col is not None and arr_col < len(row):
                            ats = clocks(row[arr_col])
                            arrival = ats[0][0] if len(ats) == 1 else None
                        for clock, _, _ in clocks(value):
                            header_dates = body_dates(h, published)
                            emit(origin, clock, joined+' [열: '+h+']', header_dates if header_dates[0] else local_dates, arrival)
                else:
                    # Transposed timetable: port names on column headers.
                    for ci, value in enumerate(row):
                        h = headers.get(ci, '')
                        origin = port_in(h)
                        if origin and not re.search(r'도착|변경\s*전|기존', h):
                            for clock, _, _ in clocks(value):
                                header_dates = body_dates(h, published)
                                emit(origin, clock, joined+' [열: '+h+']', header_dates if header_dates[0] else local_dates)
                        elif clocks(value):
                            issues.append('표의 출발지/변경 전후 불명확: '+joined)

        # Parse prose in logical blocks, preserving departure labels, never joining
        # the time under the other port as an arrival. Table text is excluded.
        def blocks(node):
            block_names = ('p', 'div', 'li', 'h1', 'h2', 'h3', 'h4')
            children = [c for c in node.children if isinstance(c, Tag)]
            if node.name in block_names and not node.find(block_names):
                return [re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip()]
            if any(node.find(name) for name in block_names):
                result = []
                for child in node.children:
                    if isinstance(child, Tag):
                        result.extend(blocks(child))
                    elif str(child).strip():
                        result.append(clean(child))
                return result
            return [re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip()]
        lines = [s for s in blocks(outside) if s]
        # "A/B까지 정상 운항. 이후 C/D ... 통제" is a scoped contrast:
        # normal voyages before the boundary must not become cancelled voyages.
        boundary = next((i for i, line in enumerate(lines) if '이후' in line), None)
        normal_before = boundary is not None and any(re.search(r'정상\s*운항', line) for line in lines[:boundary+1])
        active_origin, active_dates, section = None, None, ''
        controlled_scope = False
        for line_index, line in enumerate(lines):
            if re.search(r'변경\s*전|기존\s*(?:운항|시간표)|정상\s*운항', line):
                section = 'old'
            if re.search(r'변경\s*후|증회|추가\s*운항', line):
                section = 'new'
            ds = body_dates(line, published)
            if ds[0]:
                active_dates = ds
            if normal_before and line_index < boundary:
                continue
            if normal_before and line_index >= boundary:
                section = 'new'
                controlled_scope = True
            if section == 'old':
                continue
            labelled = []
            for p in ports:
                for m in re.finditer(re.escape(p)+r'(?:항)?\s*(?:출발|출항|발)?\s*[:：]?', line):
                    # Ignore ports in a route heading; only a following local time
                    # makes this occurrence a departure label.
                    labelled.append((m.start(), m.end(), p))
            labelled.sort()
            if not clocks(line):
                if len(labelled) == 1:
                    active_origin = labelled[0][2]
                continue
            if re.search(r'일부\s*항차|야간', text) and not controlled_scope and not re.search(r'통제|결항', line):
                issues.append('부분통제의 명시 대상 여부 불명확: '+line)
                continue
            if labelled:
                for i, (start, end, p) in enumerate(labelled):
                    segment = line[end:labelled[i+1][0] if i+1 < len(labelled) else len(line)]
                    # Only explicit departure labels may generate rows. Arrival
                    # labels are metadata for a prior departure, not another trip.
                    is_arrival = bool(re.search(r'도착|입항', segment)) and not re.search(r'출발|출항', segment)
                    if is_arrival:
                        continue
                    ts = clocks(segment)
                    arrival = None
                    if i+1 < len(labelled):
                        _, next_end, _ = labelled[i+1]
                        next_segment = line[next_end:labelled[i+2][0] if i+2 < len(labelled) else len(line)]
                        at = clocks(next_segment)
                        if re.search(r'도착|입항', next_segment) and len(at) == 1:
                            arrival = at[0][0]
                    if ts:
                        active_origin = p
                        for clock, _, _ in ts:
                            emit(p, clock, line+(' [정상운항 이후 통제 대상]' if controlled_scope else ''), active_dates, arrival)
            elif active_origin:
                for clock, _, _ in clocks(line):
                    emit(active_origin, clock, line, active_dates)
            else:
                issues.append('시각의 출발 항구 불명확: '+line)
        # Duplicate evidence may occur with merged cells. Voyage identity excludes
        # evidence, but retains source notice and type.
        unique = {tuple(row[c] for c in CORE+['게시글ID']): row for row in rows}
        rows = list(unique.values())
        if rows:
            state = '부분추출' if issues else '추출성공'
            for row in rows:
                row['추출상태'] = state
            return rows, state, '; '.join(dict.fromkeys(issues))
        if clocks(text) or soup.find('img'):
            return [], '시간표해석실패', '; '.join(dict.fromkeys(issues)) or '시각은 있으나 적용일/출발지/현재 항차 관계를 확인하지 못함'
        return [], '운항정보없음', '본문에 구체적인 항차 시각 없음'


class Store:
    """SQLite is authoritative; CSV is atomically regenerated, not appended blindly."""
    def __init__(self, directory='data'):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'notices.sqlite3'
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS notices (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, body TEXT, status TEXT NOT NULL,
                error TEXT, attempts INTEGER NOT NULL, checked_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS voyages (
                key TEXT PRIMARY KEY, notice_id TEXT NOT NULL, payload TEXT NOT NULL);
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def should_process(self, id, retry_failed=False):
        with self.connect() as db:
            row = db.execute('SELECT status,attempts FROM notices WHERE id=?', (id,)).fetchone()
        return row is None or (retry_failed and row[0] == '본문수집실패' and row[1] < 3)

    def save(self, notice, body, status, error, voyages):
        now = datetime.now(ZoneInfo('Asia/Seoul')).isoformat()
        with self.connect() as db:
            db.execute('''INSERT INTO notices VALUES(?,?,?,?,?,1,?)
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,body=excluded.body,
                status=excluded.status,error=excluded.error,attempts=notices.attempts+1,checked_at=excluded.checked_at''',
                (notice.id, json.dumps(asdict(notice), ensure_ascii=False), body, status, error, now))
            db.execute('DELETE FROM voyages WHERE notice_id=?', (notice.id,))
            for voyage in voyages:
                key = hashlib.sha256(json.dumps([voyage.get(k) for k in CORE+['게시글ID']], ensure_ascii=False).encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO voyages VALUES(?,?,?)', (key, notice.id, json.dumps(voyage, ensure_ascii=False)))

    def voyages(self):
        with self.connect() as db:
            rows = [json.loads(x[0]) for x in db.execute('SELECT payload FROM voyages ORDER BY rowid')]
        return pd.DataFrame(rows, columns=CORE+EXTRA)

    def audit(self):
        records = []
        with self.connect() as db:
            for payload, body, status, error, attempts, checked in db.execute('SELECT payload,body,status,error,attempts,checked_at FROM notices ORDER BY rowid'):
                n = json.loads(payload)
                records.append({'게시글ID': n['id'], '제목': n['title'], '상세본문 수집 성공 여부': body is not None,
                                '본문일부': BeautifulSoup(body or '', 'lxml').get_text(' ', strip=True)[:300],
                                '추출상태': status, '실패단계': '본문수집' if body is None else ('시간표해석' if status in ('시간표해석실패', '부분추출') else ''),
                                '실패이유': error or '', '시도횟수': attempts, '확인시각': checked})
        return pd.DataFrame(records, columns=['게시글ID', '제목', '상세본문 수집 성공 여부', '본문일부', '추출상태', '실패단계', '실패이유', '시도횟수', '확인시각'])

    def export(self):
        for name, frame in [('voyages', self.voyages()), ('audit', self.audit())]:
            temp = self.directory / f'{name}.csv.tmp'
            frame.to_csv(temp, index=False, encoding='utf-8-sig')
            temp.replace(self.directory / f'{name}.csv')


class Pipeline:
    def __init__(self, client=None, directory='data'):
        self.client = client or NoticeClient(debug_dir=Path(directory)/'debug')
        self.store = Store(directory)
        self.parser = VoyageParser()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.last_error = None

    def run_once(self, retry_failed=False):
        with self.lock:
            # Listing failure propagates, so a blocked site is never a successful
            # empty collection. Existing persisted rows remain intact.
            notices = self.client.listing()
            listing = pd.DataFrame([{'게시글ID': n.id, '제목': n.title, '게시일': n.published,
                                      '키워드 해당 여부': keyword_hit(n.title)} for n in notices])
            for n in notices:
                if not keyword_hit(n.title) or not self.store.should_process(n.id, retry_failed):
                    continue
                try:
                    body, debug = self.client.detail(n)
                except (requests.RequestException, CrawlError, ValueError) as e:
                    self.store.save(n, None, '본문수집실패', f'{type(e).__name__}: {e}', [])
                    continue
                try:
                    voyages, status, error = self.parser.parse(n, body)
                except Exception as e:
                    voyages, status, error = [], '시간표해석실패', f'{type(e).__name__}: {e}'
                self.store.save(n, body, status, error, voyages)
            self.store.export()
            return listing, self.store.audit(), self.store.voyages()

    def start(self, seconds=1800):
        if seconds < 60:
            raise ValueError('과도한 요청 방지를 위해 최소 60초')
        if self.thread and self.thread.is_alive():
            raise RuntimeError('이미 감시 중')
        self.stop_event.clear()
        def watch():
            while not self.stop_event.wait(seconds):
                try:
                    self.run_once(retry_failed=True)
                    self.last_error = None
                except Exception as e:
                    self.last_error = f'{datetime.now(ZoneInfo('Asia/Seoul')).isoformat()} {type(e).__name__}: {e}'
                    log = self.store.directory / 'monitor_errors.log'
                    with log.open('a', encoding='utf-8') as f:
                        f.write(self.last_error+'\n')
        self.thread = threading.Thread(target=watch, name='ship-notice-monitor', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        # Current HTTP request has a finite timeout; no new cycle starts after stop.
        if self.thread:
            self.thread.join(timeout=1)
        return not (self.thread and self.thread.is_alive())
