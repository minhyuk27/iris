"""
IRIS - KIS Open API 래퍼
"""
import json
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path

import core.config as cfg
from core.timeutil import now_kst_naive, today_kst

BASE_URL    = cfg.KIS_BASE_URL
TOKEN_PATH  = cfg.TOKEN_PATH
MAX_PER_SEC = 18

import threading

_last_calls: list = []
_call_lock   = threading.Lock()
_token_lock  = threading.Lock()
# 메모리 토큰 캐시 (멀티스레드 재발급 방지)  {"token": str, "expired": datetime}
_token_cache: dict = {}

# 2026-08-07: 체결지연 대응 리서치 — 기존엔 requests.get/post를 모듈함수로 그대로
# 호출했는데, 이 방식(requests 최상위 편의함수)은 매 호출마다 내부적으로 새
# Session을 만들어 TCP+TLS 핸드셰이크를 처음부터 다시 함. 09:30 후보 병렬조회
# (최대 50종목, ThreadPoolExecutor 10워커)나 체결확인 폴링(최대 45초간 최대
# 6회 반복조회)처럼 같은 KIS 호스트에 짧은 시간 안에 여러 번 요청하는 경로에서
# 매번 이 오버헤드를 물게 됨. Session+HTTPAdapter로 커넥션을 재사용하면 이
# 반복호출 구간의 왕복시간을 줄일 수 있다(요청/응답 처리 자체나 KIS 서버쪽
# 지연은 못 줄임 — 순수 "우리쪽 커넥션 셋업 오버헤드"만 없앰).
# pool_maxsize는 병렬조회 워커수(10)보다 넉넉히 잡아 풀 자체가 병목이 되는 걸 방지.
_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


def _rate_limit():
    with _call_lock:
        now = time.time()
        global _last_calls
        _last_calls = [t for t in _last_calls if now - t < 1.0]
        if len(_last_calls) >= MAX_PER_SEC:
            sleep_t = 1.0 - (now - _last_calls[0]) + 0.05
            if sleep_t > 0:
                time.sleep(sleep_t)
        _last_calls.append(time.time())


def _token_valid(expired) -> bool:
    return expired is not None and now_kst_naive() < expired - timedelta(minutes=5)


def _get_token() -> str:
    """
    KIS 접근토큰 조회. 멀티스레드 안전.
      1) 메모리 캐시 유효하면 즉시 반환 (락 없음)
      2) 파일 캐시 유효하면 메모리에 적재 후 반환
      3) 둘 다 만료 → 락 안에서 1스레드만 발급 (이중 검사로 중복 발급 방지)
    KIS는 토큰 발급 시마다 카톡 알림을 보내므로 발급 횟수 최소화가 중요.
    """
    # 1) 메모리 캐시
    tok = _token_cache.get("token")
    if tok and _token_valid(_token_cache.get("expired")):
        return tok

    with _token_lock:
        # 2) 락 획득 후 재확인 — 다른 스레드가 이미 발급했을 수 있음
        tok = _token_cache.get("token")
        if tok and _token_valid(_token_cache.get("expired")):
            return tok

        # 파일 캐시 확인 (다른 프로세스가 발급해둔 토큰 재사용)
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        if TOKEN_PATH.exists():
            try:
                data = json.loads(TOKEN_PATH.read_text())
                expired = datetime.strptime(
                    data["access_token_token_expired"], "%Y-%m-%d %H:%M:%S")
                if _token_valid(expired):
                    _token_cache["token"]   = data["access_token"]
                    _token_cache["expired"] = expired
                    return data["access_token"]
            except Exception:
                pass

        # 3) 신규 발급 (락 안 — 단일 스레드)
        r = _session.post(
            f"{BASE_URL}/oauth2/tokenP",
            json={"grant_type": "client_credentials",
                  "appkey": cfg.KIS_APP_KEY,
                  "appsecret": cfg.KIS_APP_SECRET},
            headers={"content-type": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        TOKEN_PATH.write_text(json.dumps(data, ensure_ascii=False))
        try:
            expired = datetime.strptime(
                data["access_token_token_expired"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            expired = now_kst_naive() + timedelta(hours=23)
        _token_cache["token"]   = data["access_token"]
        _token_cache["expired"] = expired
        return data["access_token"]


def _headers(tr_id: str) -> dict:
    return {
        "content-type":  "application/json",
        "authorization": f"Bearer {_get_token()}",
        "appkey":        cfg.KIS_APP_KEY,
        "appsecret":     cfg.KIS_APP_SECRET,
        "tr_id":         tr_id,
        "custtype":      "P",
    }


def _get(path: str, params: dict, tr_id: str) -> dict:
    _rate_limit()
    r = _session.get(f"{BASE_URL}{path}", params=params,
                     headers=_headers(tr_id), timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("rt_cd") != "0":
        raise Exception(f"KIS API 오류: {data.get('msg1')} ({data.get('msg_cd')})")
    return data


def _post(path: str, body: dict, tr_id: str) -> dict:
    _rate_limit()
    r = _session.post(f"{BASE_URL}{path}", json=body,
                      headers=_headers(tr_id), timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("rt_cd") != "0":
        raise Exception(f"KIS API 오류: {data.get('msg1')} ({data.get('msg_cd')})")
    return data


def _acc() -> tuple:
    a = cfg.KIS_ACCOUNT.replace("-", "")
    return a[:8], a[8:]


# ── 잔고 ──────────────────────────────────────────────────────

def get_available_cash() -> float:
    """
    실전 매수 가능 금액 조회 (T+2 결제 고려한 실제 가용 예수금)
    dnca_tot_amt: 예수금 총액
    prvs_rcdl_excc_amt: 가수도 정산 금액 (T+2 미결제)
    nass_amt: 순자산금액
    실제 사용 가능: max_buy_amt (매수가능금액)
    """
    acc, prd = _acc()
    tr   = "VTTC8434R" if cfg.KIS_IS_VIRTUAL else "TTTC8434R"
    data = _get(
        "/uapi/domestic-stock/v1/trading/inquire-balance",
        params={"CANO": acc, "ACNT_PRDT_CD": prd,
                "AFHR_FLPR_YN": "N", "OFL_YN": "",
                "INQR_DVSN": "02", "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""},
        tr_id=tr,
    )
    o = data["output2"][0]
    # 매수가능금액 우선, 없으면 예수금 총액
    return float(o.get("max_buy_amt", 0) or o.get("dnca_tot_amt", 0))


def get_order_psbl_qty(ticker: str, price: float) -> int:
    """
    시장가 매수 주문가능수량 조회 (inquire-psbl-order).
    시장가 주문(ORD_DVSN=01)은 KIS가 상한가(기준가×1.30) 기준으로 현금을
    예약/검증하므로, 갭업 종목은 (가용현금 / 현재가)로 계산한 수량이
    실제 주문가능수량보다 클 수 있다 (→ "주문가능금액을 초과 했습니다").
    이 API의 max_buy_qty/nrcvb_buy_qty는 KIS가 직접 상한가 기준으로
    계산한 값이라 그대로 사용하면 해당 오류를 피할 수 있다.
    """
    acc, prd = _acc()
    tr   = "VTTC8908R" if cfg.KIS_IS_VIRTUAL else "TTTC8908R"
    data = _get(
        "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
        params={"CANO": acc, "ACNT_PRDT_CD": prd,
                "PDNO": ticker, "ORD_UNPR": str(int(price)),
                "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"},
        tr_id=tr,
    )
    o = data["output"]
    return int(o.get("max_buy_qty", 0) or o.get("nrcvb_buy_qty", 0))


# ── 현재가 ────────────────────────────────────────────────────

def get_price(ticker: str) -> dict:
    data = _get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        tr_id="FHKST01010100",
    )
    o = data["output"]
    return {
        "ticker":     ticker,
        "price":      int(o["stck_prpr"]),
        "open":       int(o["stck_oprc"]),
        "high":       int(o["stck_hgpr"]),
        "low":        int(o["stck_lwpr"]),
        "prev_close": int(o["stck_sdpr"]),
        "volume":     int(o["acml_vol"]),
        "trade_value":float(o.get("acml_tr_pbmn", 0)),
        "change_pct": float(o["prdy_ctrt"]),
    }


# ── 일봉 ──────────────────────────────────────────────────────

def get_daily_ohlcv(ticker: str, start: str = "20230101") -> list:
    today = today_kst()
    data  = _get(
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":         ticker,
            "FID_INPUT_DATE_1":       start,
            "FID_INPUT_DATE_2":       today,
            "FID_PERIOD_DIV_CODE":    "D",
            "FID_ORG_ADJ_PRC":        "0",
        },
        tr_id="FHKST03010100",
    )
    result = []
    for item in data.get("output2", []):
        d = item.get("stck_bsop_date", "")
        if not d:
            continue
        try:
            result.append({
                "date":   d,
                "open":   int(item.get("stck_oprc", 0) or 0),
                "high":   int(item.get("stck_hgpr", 0) or 0),
                "low":    int(item.get("stck_lwpr", 0) or 0),
                "close":  int(item.get("stck_clpr", 0) or 0),
                "volume": int(item.get("acml_vol",  0) or 0),
            })
        except (ValueError, TypeError):
            continue
    return list(reversed(result))


def get_index_daily(idx_code: str, start: str) -> list:
    """국내 지수 일봉 시세 (idx_code: '0001'=KOSPI, '1001'=KOSDAQ).
    반환: [{"date": "YYYYMMDD", "close": float}, ...]  오름차순
    """
    today = today_kst()
    data  = _get(
        "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
        params={
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD":         idx_code,
            "FID_INPUT_DATE_1":       start,
            "FID_INPUT_DATE_2":       today,
            "FID_PERIOD_DIV_CODE":    "D",
        },
        tr_id="FHKUP03500100",
    )
    result = []
    for item in data.get("output2", []):
        d = item.get("stck_bsop_date", "")
        if not d:
            continue
        try:
            cls = float(item.get("bstp_nmix_prpr", 0) or 0)
            if cls <= 0:
                cls = float(item.get("bstp_nmix_clpr", 0) or 0)
            result.append({"date": d, "close": cls})
        except (ValueError, TypeError):
            continue
    return list(reversed(result))


# ── 수급 ──────────────────────────────────────────────────────

def get_supply_range(ticker: str, start: str, end: str) -> list:
    try:
        data = _get(
            "/uapi/domestic-stock/v1/quotations/inquire-investor",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD":         ticker,
                "FID_INPUT_DATE_1":       start,
                "FID_INPUT_DATE_2":       end,
                "FID_PERIOD_DIV_CODE":    "D",
            },
            tr_id="FHKST01010900",
        )
    except Exception:
        return []

    result = []
    for r in data.get("output", []):
        d = r.get("stck_bsop_date", "")
        if not d:
            continue
        result.append({
            "date":        d,
            "inst_net":    int(r.get("orgn_ntby_qty",     0) or 0),
            "inst_amt":    int(r.get("orgn_ntby_tr_pbmn", 0) or 0),
            "foreign_net": int(r.get("frgn_ntby_qty",     0) or 0),
            "foreign_amt": int(r.get("frgn_ntby_tr_pbmn", 0) or 0),
        })
    return result


# ── 유니버스 수집 (거래량 순위) ──────────────────────────────

def get_liquid_stocks(market: str = "KOSPI") -> list:
    """
    KIS 거래량 순위로 유동성 종목 수집
    KOSPI: mkt_code=0001, KOSDAQ: mkt_code=1001
    둘 다 FID_COND_MRKT_DIV_CODE="J" 사용
    """
    mkt_code = "0001" if market == "KOSPI" else "1001"
    all_stocks: dict = {}

    price_ranges = [
        ("1000",   "5000"),
        ("5000",   "30000"),
        ("30000",  "100000"),
        ("100000", "300000"),
        ("300000", ""),
        ("0",      "1000"),
    ]

    for p1, p2 in price_ranges:
        try:
            data = _get(
                "/uapi/domestic-stock/v1/quotations/volume-rank",
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",   # KOSPI/KOSDAQ 모두 J
                    "FID_COND_SCR_DIV_CODE":  "20171",
                    "FID_INPUT_ISCD":         mkt_code,
                    "FID_DIV_CLS_CODE":       "0",
                    "FID_BLNG_CLS_CODE":      "0",
                    "FID_TRGT_CLS_CODE":      "111111111",
                    "FID_TRGT_EXLS_CLS_CODE": "000000000",
                    "FID_INPUT_PRICE_1":      p1,
                    "FID_INPUT_PRICE_2":      p2,
                    "FID_VOL_CNT":            "0",
                    "FID_INPUT_DATE_1":       "",
                },
                tr_id="FHPST01710000",
            )
            for item in data.get("output", []):
                code = item.get("mksc_shrn_iscd", "")
                name = item.get("hts_kor_isnm", "")
                if code and code.isdigit() and len(code) == 6 and code not in all_stocks:
                    all_stocks[code] = {
                        "ticker": code,
                        "name":   name,
                        "market": market,
                        "sector": "기타",
                    }
            time.sleep(0.07)
        except Exception as e:
            pass

    return list(all_stocks.values())


# ── 주문 ──────────────────────────────────────────────────────

def buy_market(ticker: str, qty: int) -> dict:
    acc, prd = _acc()
    tr = "VTTC0802U" if cfg.KIS_IS_VIRTUAL else "TTTC0802U"
    return _post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        body={"CANO": acc, "ACNT_PRDT_CD": prd, "PDNO": ticker,
              "ORD_DVSN": "01", "ORD_QTY": str(qty),
              "ORD_UNPR": "0", "CTAC_TLNO": "", "SLL_TYPE": "01", "ALGO_NO": ""},
        tr_id=tr,
    )


def sell_market(ticker: str, qty: int) -> dict:
    acc, prd = _acc()
    tr = "VTTC0801U" if cfg.KIS_IS_VIRTUAL else "TTTC0801U"
    return _post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        body={"CANO": acc, "ACNT_PRDT_CD": prd, "PDNO": ticker,
              "ORD_DVSN": "01", "ORD_QTY": str(qty),
              "ORD_UNPR": "0", "CTAC_TLNO": "", "ALGO_NO": ""},
        tr_id=tr,
    )


# ── 지정가 주문·취소/정정(2026-08-07 신규, ④지정가 주문 리서치) ──────────────
# tr_id는 buy_market/sell_market과 같은 "구세대" 계열로 통일(TTTC0802U/0801U와
# 같은 세대) — KIS API가 매수·매도·정정취소·조회 전부에 걸쳐 신구세대로 갈려있고
# (신세대는 EXCG_ID_DVSN_CD 필수, NXT/SOR 복수거래소 대응 추정) iris는 이미
# 매수/매도에 구세대를 실거래로 검증된 채 쓰고 있어, 정정취소·조회만 신세대로
# 섞으면 검증 안 된 세대간 호환성 리스크를 새로 만드는 셈이라 구세대로 통일.
# 확인 경위: iris DECISIONS 2026-08-07 (10차)·(11차)·(12차) — 공식 GitHub
# 저장소(koreainvestment/open-trading-api)의 legacy/ 폴더 + "실전계좌 POSTMAN
# 샘플코드 v2.6"(정본 레퍼런스) 직접대조로 검증, 모의투자 실호출 없이 문서만으로 확정.

def buy_limit(ticker: str, qty: int, price: int) -> dict:
    """지정가 매수(ORD_DVSN=00). price는 반드시 호가단위에 맞는 값이어야 함
    (core/order_exec.py의 round_to_tick() 참조 — 안 맞으면 KIS가 주문을 거부함)."""
    acc, prd = _acc()
    tr = "VTTC0802U" if cfg.KIS_IS_VIRTUAL else "TTTC0802U"
    return _post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        body={"CANO": acc, "ACNT_PRDT_CD": prd, "PDNO": ticker,
              "ORD_DVSN": "00", "ORD_QTY": str(qty),
              "ORD_UNPR": str(int(price)), "CTAC_TLNO": "", "SLL_TYPE": "01", "ALGO_NO": ""},
        tr_id=tr,
    )


def buy_conditional_limit(ticker: str, qty: int, price: int) -> dict:
    """조건부지정가 매수(ORD_DVSN=02, iris v3 전용). 정규장 중에는 지정가와
    동일하게 참여하다가, 미체결 잔량은 장종료 전 10분간(15:20~15:30) 단일가
    매매(동시호가)에서 거래소가 자동으로 시장가로 전환함(KRX 표준 기능 —
    별도 취소·정정 불필요). price는 buy_limit과 동일하게 호가단위 필요.
    가격제한폭 확대 국면·VI 발동 시 사용 불가(iris DECISIONS 2026-08-18
    (21차) 조사 참조). tr_id는 buy_limit/buy_market과 같은 구세대 계열."""
    acc, prd = _acc()
    tr = "VTTC0802U" if cfg.KIS_IS_VIRTUAL else "TTTC0802U"
    return _post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        body={"CANO": acc, "ACNT_PRDT_CD": prd, "PDNO": ticker,
              "ORD_DVSN": "02", "ORD_QTY": str(qty),
              "ORD_UNPR": str(int(price)), "CTAC_TLNO": "", "SLL_TYPE": "01", "ALGO_NO": ""},
        tr_id=tr,
    )


def sell_limit(ticker: str, qty: int, price: int) -> dict:
    """지정가 매도(ORD_DVSN=00). price는 호가단위 반올림 필요(위 buy_limit 참조)."""
    acc, prd = _acc()
    tr = "VTTC0801U" if cfg.KIS_IS_VIRTUAL else "TTTC0801U"
    return _post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        body={"CANO": acc, "ACNT_PRDT_CD": prd, "PDNO": ticker,
              "ORD_DVSN": "00", "ORD_QTY": str(qty),
              "ORD_UNPR": str(int(price)), "CTAC_TLNO": "", "ALGO_NO": ""},
        tr_id=tr,
    )


def get_cancelable_order(odno: str) -> dict | None:
    """정정취소가능주문조회(inquire-psbl-rvsecncl) — 미체결 지정가 주문의
    취소/정정 가능수량(psbl_qty)·주문점(KRX_FWDG_ORD_ORGNO)을 조회.

    **실전계좌 전용으로 확인됨** — 모의투자(KIS_IS_VIRTUAL) 대응 tr_id가
    공식 모의계좌 POSTMAN 컬렉션에 존재하지 않아(iris DECISIONS 2026-08-07
    (12차)) KIS_IS_VIRTUAL 분기를 두지 않았음. 모의투자에서 호출하면 실패할
    수 있음 — paper 모드(TRADE_MODE=paper)는 애초에 이 함수를 호출할 일이
    없으므로(실주문 자체가 없음) 실사용엔 영향 없음.

    odno에 해당하는 주문을 못 찾으면(이미 전량체결·취소 등) None 반환 —
    호출부는 이 경우를 "더 이상 처리할 미체결분이 없음"으로 다뤄야 함."""
    acc, prd = _acc()
    tr = "TTTC8036R"
    data = _get(
        "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
        params={"CANO": acc, "ACNT_PRDT_CD": prd,
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
                "INQR_DVSN_1": "0", "INQR_DVSN_2": "0"},
        tr_id=tr,
    )
    for item in data.get("output", []):
        if item.get("odno") == odno:
            return {
                "psbl_qty": int(item.get("psbl_qty", 0) or 0),
                "krx_fwdg_ord_orgno": item.get("ord_gno_brno", ""),
                "ord_dvsn": item.get("ord_dvsn_cd", ""),
                "pdno": item.get("pdno", ""),
            }
    return None


def cancel_order(odno: str, krx_fwdg_ord_orgno: str, qty: int) -> dict:
    """미체결 지정가 주문 취소(잔량전부, QTY_ALL_ORD_YN=Y). qty는
    get_cancelable_order()의 psbl_qty를 그대로 넣을 것 — "Y"여도 ORD_QTY에
    실수량을 채워야 함이 공식 요청예제로 확인됨(iris DECISIONS 2026-08-07
    (12차))."""
    acc, prd = _acc()
    tr = "VTTC0803U" if cfg.KIS_IS_VIRTUAL else "TTTC0803U"
    return _post(
        "/uapi/domestic-stock/v1/trading/order-rvsecncl",
        body={"CANO": acc, "ACNT_PRDT_CD": prd,
              "KRX_FWDG_ORD_ORGNO": krx_fwdg_ord_orgno, "ORGN_ODNO": odno,
              "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02",
              "ORD_QTY": str(qty), "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y"},
        tr_id=tr,
    )


def revise_to_market(odno: str, krx_fwdg_ord_orgno: str, qty: int) -> dict:
    """미체결 지정가 잔량을 시장가로 정정(취소 후 재주문 대신 정정 1회로 처리).
    KIS 공식문서에 "정정은 원주문에 대한 주문단가 혹은 주문구분을 변경하는
    사항"이라 명시돼있어 주문구분(지정가→시장가) 전환이 가능함을 확인
    (iris DECISIONS 2026-08-07 (12차)). qty는 get_cancelable_order()의
    psbl_qty를 그대로 사용."""
    acc, prd = _acc()
    tr = "VTTC0803U" if cfg.KIS_IS_VIRTUAL else "TTTC0803U"
    return _post(
        "/uapi/domestic-stock/v1/trading/order-rvsecncl",
        body={"CANO": acc, "ACNT_PRDT_CD": prd,
              "KRX_FWDG_ORD_ORGNO": krx_fwdg_ord_orgno, "ORGN_ODNO": odno,
              "ORD_DVSN": "01", "RVSE_CNCL_DVSN_CD": "01",
              "ORD_QTY": str(qty), "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y"},
        tr_id=tr,
    )


def get_filled_price(odno: str, ticker: str) -> float | None:
    """주문번호로 실제 체결가 조회. 미체결이면 None 반환."""
    acc, prd = _acc()
    tr = "VTTC8001R" if cfg.KIS_IS_VIRTUAL else "TTTC8001R"
    from datetime import date
    today = date.today().strftime("%Y%m%d")
    try:
        data = _get(
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            params={"CANO": acc, "ACNT_PRDT_CD": prd,
                    "INQR_STRT_DT": today, "INQR_END_DT": today,
                    "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00",
                    "PDNO": ticker, "CCLD_DVSN": "01",
                    "ORD_GNO_BRNO": "", "ODNO": odno,
                    "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
                    "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""},
            tr_id=tr,
        )
        for item in data.get("output1", []):
            price = float(item.get("avg_prvs", 0) or item.get("ccld_unpr", 0))
            if price > 0:
                return price
    except Exception:
        pass
    return None


def get_order_fill_qty(odno: str, ticker: str) -> int | None:
    """주문번호로 실제 체결수량 직접 조회(2026-08-07 신규, order_exec.py의 잔고폴링
    fast-path용) — get_filled_price와 같은 엔드포인트(inquire-daily-ccld, 주문체결
    원장 자체를 조회)라 잔고 반영 지연과 무관하게 더 빨리 알 수 있는 경우가 있다
    (2026-08-07 halt 사고: 45초 잔고폴링으로는 못 밝혀낸 체결이 get_filled_price로는
    바로 조회됨 — 같은 원장을 보는 이 함수도 마찬가지일 것으로 기대).
    조회 실패/미체결/필드 파싱 불가 등 확신 없는 모든 경우 None(호출부는 반드시
    잔고폴링과 병행해 이 값이 틀렸거나 없어도 안전하게 폴백해야 함 — 이 함수
    하나만 믿고 체결확정 판단을 내리지 말 것)."""
    acc, prd = _acc()
    tr = "VTTC8001R" if cfg.KIS_IS_VIRTUAL else "TTTC8001R"
    from datetime import date
    today = date.today().strftime("%Y%m%d")
    try:
        data = _get(
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            params={"CANO": acc, "ACNT_PRDT_CD": prd,
                    "INQR_STRT_DT": today, "INQR_END_DT": today,
                    "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00",
                    "PDNO": ticker, "CCLD_DVSN": "01",
                    "ORD_GNO_BRNO": "", "ODNO": odno,
                    "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
                    "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""},
            tr_id=tr,
        )
        for item in data.get("output1", []):
            qty = item.get("tot_ccld_qty")
            if qty is None:
                continue
            return int(qty)
    except Exception:
        pass
    return None


def get_minute_ohlcv(ticker: str, base_time: str = "153000") -> list:
    """
    1분봉 조회 (역순, 최대 30개)
    base_time: HHMMSS 기준시각부터 과거 방향으로 조회
    반환: [{'dt': YYYYMMDDHHMMSS, 'open', 'high', 'low', 'close', 'volume'}, ...]
    """
    try:
        data = _get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            params={
                "FID_ETC_CLS_CODE":      "",
                "FID_COND_MRKT_DIV_CODE":"J",
                "FID_INPUT_ISCD":        ticker,
                "FID_INPUT_HOUR_1":      base_time,
                "FID_PW_DATA_INCU_YN":   "N",
            },
            tr_id="FHKST03010200",
        )
    except Exception:
        return []

    result = []
    for item in data.get("output2", []):
        date_str = item.get("stck_bsop_date", "")
        t        = item.get("stck_cntg_hour", "")
        if not t or not date_str:
            continue
        try:
            result.append({
                "dt":     date_str + t,
                "open":   int(item.get("stck_oprc", 0) or 0),
                "high":   int(item.get("stck_hgpr", 0) or 0),
                "low":    int(item.get("stck_lwpr", 0) or 0),
                "close":  int(item.get("stck_prpr", 0) or 0),
                "volume": int(item.get("cntg_vol",  0) or 0),
            })
        except (ValueError, TypeError):
            continue
    return result


# ── 거래일 조회 ───────────────────────────────────────────────

def get_trading_days(yyyymm: str) -> list[str]:
    """
    해당 월의 KRX 거래일 목록 반환 (YYYYMMDD 문자열 리스트).
    bot_state 'trading_days_YYYYMM' 키로 캐싱 → 월 1회만 API 호출.

    KIS CTCA0903R (국내 휴장일 조회):
      BASS_DT  : 조회 기준일 — 이 날짜부터 약 24일치 창을 반환(월 전체 아님)
      bzdy_yn  : Y = 거래일, N = 휴장/공휴일

    2026-07-11 버그 수정: CTX_AREA_NK/FK를 그대로 돌려보내도 이 엔드포인트는
    페이지가 전혀 진행되지 않고 첫 응답과 동일한 창을 그대로 반복 반환한다(실측
    확인됨) — 즉 이 두 필드는 이 엔드포인트에서 사실상 무의미. 대신 매 응답의
    마지막 날짜+1일을 다음 BASS_DT로 넘겨 창을 밀어야 월 전체를 커버한다.
    이 버그로 한동안 매달 하순 며칠(대략 24일 이후)이 "휴장"으로 잘못 캐싱됐었음
    (예: 2026-07-27~31, 2026-08-25~31이 전부 실제로는 거래일인데 휴장으로 잘못 처리).
    """
    from core.db import state_get, state_set

    cache_key = f"trading_days_{yyyymm}"
    cached = state_get(cache_key)
    if isinstance(cached, list) and cached:
        return cached

    base_dt = yyyymm + "01"
    all_days: list[str] = []
    seen: set[str] = set()

    for _page in range(10):  # 최대 10페이지 — 무한루프 방지
        try:
            data = _get(
                "/uapi/domestic-stock/v1/quotations/chk-holiday",
                params={"BASS_DT": base_dt, "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
                tr_id="CTCA0903R",
            )
        except Exception as e:
            raise RuntimeError(f"거래일 조회 실패 ({yyyymm}): {e}") from e

        items = data.get("output", [])
        if not items:
            break

        last_date = None
        rolled_over = False
        for item in items:
            d  = item.get("bass_dt", "")
            yn = item.get("bzdy_yn", "N")
            if not d:
                continue
            last_date = d
            if d[:6] > yyyymm:          # 다음 달로 넘어가면 중단
                rolled_over = True
                break
            if d[:6] == yyyymm and yn == "Y" and d not in seen:
                all_days.append(d)
                seen.add(d)

        if rolled_over or last_date is None:
            break

        next_dt = (datetime.strptime(last_date, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
        if next_dt == base_dt:          # 진행 안 되면 무한루프 방지
            break
        base_dt = next_dt

    if all_days:
        state_set(cache_key, sorted(all_days))
    return sorted(all_days)


def is_trading_day(date_str: str) -> bool:
    """
    특정 날짜(YYYYMMDD)가 KRX 거래일이면 True.
    API 오류 시 토/일만 제외하는 폴백 사용.

    2026-08-18: `with ThreadPoolExecutor(...)`는 `__exit__`이 기본적으로
    shutdown(wait=True)를 불러 타임아웃 뒤에도 백그라운드 스레드가 실제로
    끝날 때까지 블로킹했다(get_trading_days가 최대 10페이지 순차HTTP호출,
    최악 ~100초) — 30초를 못 지키던 원인(08-10(17차) 발견1). with문을 없애고
    shutdown(wait=False)로 명시 처리해 타임아웃 즉시 반환하도록 수정.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
    from datetime import datetime as _dt

    def _check():
        return date_str in get_trading_days(date_str[:6])

    ex = ThreadPoolExecutor(max_workers=1)
    try:
        future = ex.submit(_check)
        return future.result(timeout=30)  # 30초 초과 시 폴백
    except (FuturesTimeout, Exception):
        return _dt.strptime(date_str, "%Y%m%d").weekday() < 5
    finally:
        ex.shutdown(wait=False)  # 백그라운드 스레드 완료를 기다리지 않고 즉시 반환


def get_balance() -> dict:
    """잔고 조회 → {'cash': float, 'final_cash': float, 'holdings': [...]}

    cash: 예수금총액(dnca_tot_amt) — T+2 미반영이라 당일 매매손익이 아직 안
    잡힌, 항상 더 크거나 같은 값. order_exec.py의 안전마진 폴백이 "더 큰 값"을
    보수적으로 가정하고 쓰므로 이 필드의 의미는 그대로 유지한다(바꾸면 안 됨).

    final_cash: 가수도정산금액(prvs_rcdl_excc_amt) — 당일 체결분까지 이미
    정산된 것으로 가정한 값. 성과 리포트(daily_report)의 "현재 잔고" 표시는
    이 필드를 써야 당일 실현손익이 즉시 반영된다(2026-07-28: cash 기준으로는
    T+2 지연 때문에 당일 -1,300원 손실이 잔고에 안 보이는 문제 발견).
    """
    acc, prd = _acc()
    tr   = "VTTC8434R" if cfg.KIS_IS_VIRTUAL else "TTTC8434R"
    data = _get(
        "/uapi/domestic-stock/v1/trading/inquire-balance",
        params={"CANO": acc, "ACNT_PRDT_CD": prd,
                "AFHR_FLPR_YN": "N", "OFL_YN": "",
                "INQR_DVSN": "02", "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""},
        tr_id=tr,
    )
    holdings = []
    for item in data["output1"]:
        qty = int(item.get("hldg_qty", 0))
        if qty > 0:
            holdings.append({
                "ticker":    item["pdno"],
                "name":      item["prdt_name"],
                "qty":       qty,
                "avg_price": float(item.get("pchs_avg_pric", 0)),
                "cur_price": float(item.get("prpr", 0)),
                "pnl_pct":   float(item.get("evlu_pfls_rt", 0)),
            })
    o2 = data["output2"][0]
    return {
        "cash":       float(o2.get("dnca_tot_amt", 0)),
        "final_cash": float(o2.get("prvs_rcdl_excc_amt", 0) or o2.get("dnca_tot_amt", 0)),
        "holdings":   holdings,
    }
